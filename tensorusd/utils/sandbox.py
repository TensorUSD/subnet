"""
Orchestrates the single-phase Docker sandbox evaluation of a miner agent.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003

import docker
import docker.errors
import docker.types
from docker.models.containers import Container  # noqa: TC002
from docker.models.networks import Network  # noqa: TC002

from tensorusd.auth.config import settings
from tensorusd.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class SandboxResult:
    success: bool
    output_csv_bytes: bytes | None = None
    stderr: str = ""
    exit_code: int | None = None
    token_budget: int | None = None
    cost_budget_usd: float | None = None


class SandboxRunner:
    """
    Manages the full sandbox lifecycle for a single agent evaluation.

    The agent runs in a single Docker container on an isolated bridge network.
    """

    def __init__(self) -> None:
        self._client = docker.from_env()
        self.allowed_hosts = settings.sandbox_allowed_hosts
        self.allowed_models = settings.sandbox_allowed_models
        self.token_budget = settings.sandbox_token_budget
        self.cost_budget_usd = settings.sandbox_cost_budget_usd

    def run(self, agent_bytes: bytes) -> SandboxResult:
        """
        Execute the sandbox evaluation for the given agent source code.
        """
        run_id = uuid.uuid4().hex[:12]
        work_dir = settings.sandbox_workdir / run_id
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            return self._run_in_workdir(agent_bytes, work_dir, run_id)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            log.debug("Cleaned up sandbox workdir: %s", work_dir)

    # Lifecycle

    def _run_in_workdir(
        self,
        agent_bytes: bytes,
        work_dir: Path,
        run_id: str,
    ) -> SandboxResult:
        # Prepare host directories
        agent_path = work_dir / "agent.py"
        cache_dir = work_dir / "cache"
        output_dir = work_dir / "output"

        agent_path.write_bytes(agent_bytes)
        cache_dir.mkdir()
        output_dir.mkdir()

        # Create the sandbox network (bridge, internet access via NAT)
        # NOTE: General internet access should be constrained by the host-level
        # egress policy or proxy layer using settings.sandbox_allowed_hosts.
        network = self._create_sandbox_network(run_id)

        container: Container | None = None

        try:
            container = self._run_container(
                run_id=run_id,
                agent_path=agent_path,
                cache_dir=cache_dir,
                output_dir=output_dir,
                network=network.name,
            )

            # Block until done or timeout
            try:
                result = container.wait(timeout=settings.sandbox_timeout)
                exit_code: int = result.get("StatusCode", -1)
            except Exception:
                last_logs = container.logs(tail=200).decode(errors="replace")
                log.warning(
                    "[%s] Container timed out after %ds", run_id, settings.sandbox_timeout
                )
                log.warning("Container logs: %s", last_logs)
                try:  # noqa: SIM105
                    container.kill()
                except Exception:
                    pass
                return SandboxResult(
                    success=False,
                    stderr="Timed out",
                    exit_code=-1,
                    token_budget=self.token_budget,
                    cost_budget_usd=self.cost_budget_usd,
                )

            logs = container.logs(stderr=True, stdout=True).decode(errors="replace")
            log.debug(
                "[%s] Container exited %d (logs=%d chars)",
                run_id,
                exit_code,
                len(logs),
            )

            if exit_code != 0:
                log.warning("[%s] Container failed (exit=%d): %s", run_id, exit_code, logs[-500:])
                return SandboxResult(
                    success=False,
                    stderr=logs,
                    exit_code=exit_code,
                    token_budget=self.token_budget,
                    cost_budget_usd=self.cost_budget_usd,
                )

            output_csv = output_dir / "output.csv"
            if not output_csv.exists():
                log.warning("[%s] Container succeeded but output.csv not found.", run_id)
                return SandboxResult(
                    success=False,
                    stderr="output.csv not produced",
                    exit_code=exit_code,
                    token_budget=self.token_budget,
                    cost_budget_usd=self.cost_budget_usd,
                )

            output_csv_bytes = output_csv.read_bytes()
            log.info("[%s] Sandbox evaluation completed successfully.", run_id)
            return SandboxResult(
                success=True,
                output_csv_bytes=output_csv_bytes,
                stderr=logs,
                exit_code=exit_code,
                token_budget=self.token_budget,
                cost_budget_usd=self.cost_budget_usd,
            )

        except docker.errors.ImageNotFound:
            log.error(
                "Sandbox image '%s' not found.  Build it first: "
                "docker build -f Dockerfile.sandbox -t %s .",
                settings.sandbox_image,
                settings.sandbox_image,
            )
            return SandboxResult(
                success=False,
                stderr="Image not found",
                exit_code=-1,
                token_budget=self.token_budget,
                cost_budget_usd=self.cost_budget_usd,
            )

        except docker.errors.APIError as exc:
            log.error("[%s] Docker API error: %s", run_id, exc)
            return SandboxResult(
                success=False,
                stderr=str(exc),
                exit_code=-1,
                token_budget=self.token_budget,
                cost_budget_usd=self.cost_budget_usd,
            )

        except Exception as exc:
            log.error("[%s] Unexpected error: %s", run_id, exc, exc_info=True)
            return SandboxResult(
                success=False,
                stderr=str(exc),
                exit_code=-1,
                token_budget=self.token_budget,
                cost_budget_usd=self.cost_budget_usd,
            )

        finally:
            # Remove the container
            if container is not None:
                try:  # noqa: SIM105
                    container.remove(force=True)
                except Exception:
                    pass

            # Remove the network
            self._remove_network(network, run_id)

    # Network helpers

    def _create_sandbox_network(self, run_id: str) -> Network:
        """
        Create a short-lived bridge network for the sandbox container.

        Labels are attached so that orphaned networks (e.g. from a validator
        crash) can be identified and cleaned up:
            docker network ls --filter label=tensorusd.managed=true
        """
        network_name = f"tensorusd-sandbox-{run_id}"
        log.debug("[%s] Creating sandbox network: %s", run_id, network_name)

        network: Network = self._client.networks.create(
            name=network_name,
            driver="bridge",
            internal=False,
            check_duplicate=True,
            labels={
                "tensorusd.managed": "true",
                "tensorusd.run_id": run_id,
                "tensorusd.phase": "sandbox",
            },
            options={
                "com.docker.network.bridge.enable_icc": "false",
                "com.docker.network.bridge.host_binding_ipv4": "0.0.0.0",
            },
        )

        log.debug("[%s] Network created: %s (id=%s)", run_id, network_name, network.id[:12])
        return network

    def is_allowed_model(self, model_name: str) -> bool:
        """Return True when the requested model is within the configured allowlist."""
        return model_name in self.allowed_models

    def is_allowed_host(self, host: str) -> bool:
        """Return True when the requested host is within the configured allowlist."""
        if host in self.allowed_hosts:
            return True
        return any(pattern.startswith("*.") and host.endswith(pattern[1:]) for pattern in self.allowed_hosts)

    def _remove_network(self, network: Network, run_id: str) -> None:
        """Silently remove a network, logging any errors."""
        try:
            network.reload()
            for container in network.containers:
                try:  # noqa: SIM105
                    network.disconnect(container, force=True)
                except Exception:
                    pass
            network.remove()
            log.debug("[%s] Network removed: %s", run_id, network.name)
        except docker.errors.NotFound:
            pass
        except Exception as exc:
            log.warning("[%s] Could not remove network %s: %s", run_id, network.name, exc)

    # Container runner

    def _run_container(
        self,
        *,
        run_id: str,
        agent_path: Path,
        cache_dir: Path,
        output_dir: Path,
        network: str,
    ) -> Container:
        """
        Spin up the sandbox container and return it (detached).
        The caller is responsible for waiting on and removing the container.
        """
        volumes: dict[str, dict[str, str]] = {
            str(agent_path): {
                "bind": "/agent/agent.py",
                "mode": "ro",
            },
            str(cache_dir): {
                "bind": "/cache",
                "mode": "rw",
            },
            str(output_dir): {
                "bind": "/data/output",
                "mode": "rw",
            },
        }

        container_name = f"tensorusd-sandbox-{run_id}"

        # GPU — only request if nvidia runtime is present on the host.
        device_requests: list[docker.types.DeviceRequest] = []
        try:
            runtimes = self._client.info().get("Runtimes", {})
            if "nvidia" in runtimes:
                device_requests = [docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])]
        except Exception:
            pass

        nano_cpus = int(settings.cpu_limit * 1_000_000_000)

        container: Container = self._client.containers.run(
            image=settings.sandbox_image,
            command="python /agent/agent.py --infer",
            name=container_name,
            detach=True,
            remove=False,
            network=network,
            mem_limit=settings.memory_limit,
            nano_cpus=nano_cpus,
            device_requests=device_requests,
            volumes=volumes,
        )

        log.debug("[%s] Container started: %s", run_id, container_name)
        return container