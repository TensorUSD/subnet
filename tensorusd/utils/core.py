"""
Main validator evaluation loop.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import bittensor as bt
import requests

from tensorusd.auth.config import settings
from tensorusd.utils.logging import get_logger
from tensorusd.utils.backend_client import BackendClient
from tensorusd.utils.agent_cache import BestAgentCache, BestAgentWatcher
from tensorusd.utils.sandbox import SandboxRunner
from tensorusd.utils.scored_cache import ScoredCache
from tensorusd.validator.agent_evaluation import get_score
from tensorusd.utils.security import validate_agent_file, validate_agent_format
from tensorusd.utils.weight_setter import WeightSetter

log = get_logger(__name__)


class ValidatorCore:
    """
    Orchestrates the full validator evaluation loop.
    """

    def __init__(self, wallet: bt.Wallet) -> None:
        self._wallet = wallet
        # self._benchmark_dir = benchmark_data_dir

        # Bittensor chain connections
        self._subtensor = bt.Subtensor(network=settings.network)
        self._metagraph = bt.Metagraph(netuid=settings.netuid, network=settings.network)

        # Core components
        self._client = BackendClient(wallet)
        self._cache = BestAgentCache()
        self._watcher = BestAgentWatcher(self._client, self._cache)
        self._sandbox = SandboxRunner()
        self._weight_setter = WeightSetter(self._wallet, self._subtensor, self._metagraph)

        # Persistent, bounded cache of already-scored submission IDs.
        # Survives validator restarts; auto-evicts oldest entries at max_size.
        self._scored = ScoredCache(
            path=settings.scored_cache_path,
            max_size=settings.scored_cache_max_size,
        )

    # Lifecycle

    def start(self) -> None:
        """Start the background watcher and run the evaluation loop forever."""
        log.info(
            "Validator starting — network=%s  netuid=%d  hotkey=%s",
            settings.network,
            settings.netuid,
            self._wallet.hotkey.ss58_address,
        )
        self._watcher.start()
        self._weight_setter.start_monitor()

        try:
            self._run_loop()
        except KeyboardInterrupt:
            log.info("Received keyboard interrupt, shutting down.")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        self._watcher.stop()
        self._weight_setter.stop_monitor()
        self._client.close()
        log.info("Validator stopped.")

    # Main loop

    def _run_loop(self) -> None:
        while True:
            try:
                self._evaluation_cycle()
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 403:
                    log.error(
                        "Received 403 from backend — this hotkey has no validator permit.\n"
                        "  Shutting down. Run with a registered validator hotkey."
                    )
                    raise SystemExit(1)  # noqa: B904
                log.error("Unexpected HTTP error in evaluation cycle: %s", exc, exc_info=True)
                time.sleep(settings.validator_poll_interval)
            except Exception as exc:
                # Log but never crash — always keep running
                log.error("Unexpected error in evaluation cycle: %s", exc, exc_info=True)
                time.sleep(settings.validator_poll_interval)

    def _evaluation_cycle(self) -> None:
        """One full pass: poll → validate → evaluate → score → (maybe) set weights."""

        # Poll for next submission
        submission = self._client.get_unevaluated_submission()

        if submission is None:
            log.info("No unevaluated submissions.  Sleeping %ds.", settings.validator_poll_interval)
            self._maybe_set_weights()
            time.sleep(settings.validator_poll_interval)
            return

        log.debug("Raw submission response: %s", submission)
        sub_id: str = submission.get("submission_id") or submission.get("id")
        if not sub_id:
            log.error(
                "Submission response missing id field. Keys received: %s",
                list(submission.keys()),
            )
            return

        # Check persistent scored cache — survives restarts
        if sub_id in self._scored:
            log.warning(
                "Submission %s already scored by this validator — skipping.",
                sub_id,
            )
            time.sleep(settings.validator_poll_interval)
            return

        miner_hotkey: str | None = (
            submission.get("miner_hotkey") or submission.get("hotkey") or None
        )
        score_count: int = submission.get("score_count", 0)
        log.info(
            "Evaluating submission %s from %s (scores so far: %d/20)",
            sub_id,
            miner_hotkey or "unknown",
            score_count,
        )

        # Download agent file
        try:
            agent_bytes = self._client.download_submission_file(sub_id)
        except Exception as exc:
            log.error("Failed to download submission %s: %s", sub_id, exc)
            self._client.post_score(sub_id, 0.0)
            return

        # Security validation — pure Python, binary check, AST scan, token scan.
        # This replaces the old regex-based _detect_malware().
        security_reason = validate_agent_file(agent_bytes)
        if security_reason:
            log.warning("Security check failed for %s: %s", sub_id, security_reason)
            try:
                self._client.blacklist_miner(miner_hotkey, sub_id, f"security: {security_reason}")
            except Exception as bl_exc:
                log.error("Blacklist call failed (continuing): %s", bl_exc)
            self._client.post_score(sub_id, 0.0)
            self._scored.add(sub_id)
            return

        format_reason = validate_agent_format(agent_bytes)
        if format_reason:
            log.warning("Format check failed for %s: %s", sub_id, format_reason)
            self._client.post_score(sub_id, 0.0)
            self._scored.add(sub_id)
            return

        # Plagiarism check
        #    validate_agent_file guarantees the file is valid Python, so we can
        #    safely decode it here.
        agent_source = agent_bytes.decode("utf-8")
        plagiarism_reason = self._check_plagiarism(agent_source)
        if plagiarism_reason:
            log.warning("Plagiarism detected in %s: %s", sub_id, plagiarism_reason)
            try:
                self._client.blacklist_miner(
                    miner_hotkey, sub_id, f"plagiarism: {plagiarism_reason}"
                )
            except Exception as bl_exc:
                log.error("Blacklist call failed (continuing): %s", bl_exc)
            self._client.post_score(sub_id, 0.0)
            self._scored.add(sub_id)
            return

        # Sandbox evaluation
        log.info("Running sandbox for submission %s", sub_id)
        sandbox_result = self._sandbox.run(agent_bytes)

        if not sandbox_result.success or sandbox_result.output_csv_bytes is None:
            log.warning(
                "Sandbox failed for %s (exit=%s): %s",
                sub_id,
                sandbox_result.exit_code,
                sandbox_result.stderr[:300],
            )
            self._client.post_score(sub_id, 0.0)
            self._scored.add(sub_id)
            self._maybe_set_weights()
            return

        # Fetch ground truth fresh into memory
        try:
            ground_truth_bytes = self._client.download_ground_truth(settings.benchmark_type)
            log.debug("Ground truth downloaded into memory (%d bytes).", len(ground_truth_bytes))
        except Exception as exc:
            log.error("Ground truth download failed: %s — skipping cycle.", exc)
            # Don't penalise the miner for our own failure — do NOT mark as scored
            return

        # Score
        output_csv_bytes = sandbox_result.output_csv_bytes
        scorer = get_score(settings.benchmark_type)
        score_result = scorer(output_csv_bytes, ground_truth_bytes)

        # Post score and mark as done
        try:
            self._client.post_score(sub_id, score_result.final_score)
            self._scored.add(sub_id)  # mark as scored so we never re-evaluate
        except Exception as exc:
            log.error("Failed to post score for %s: %s", sub_id, exc)
            return

        # set weights
        self._maybe_set_weights(hint_hotkey=miner_hotkey)

    # Helpers

    def _maybe_set_weights(self, hint_hotkey: str | None = None) -> None:
        """
        Attempt to set on-chain weights.
        """
        best_hotkey: str | None = hint_hotkey
        log.info("_maybe_set_weights called — hint_hotkey=%s", hint_hotkey or "none")

        if not best_hotkey:
            best_meta = self._cache.best_meta
            best_hotkey = best_meta.hotkey if best_meta else None
            log.info("  → from cache: %s", best_hotkey or "none")

        if not best_hotkey:
            meta = self._client.get_best_submission_meta()
            if meta:
                best_hotkey = meta.get("hotkey") or meta.get("miner_hotkey")
            log.info("  → from backend: %s", best_hotkey or "none")

        if not best_hotkey:
            log.info("No best hotkey found anywhere — skipping weight set.")
            return

        try:
            self._weight_setter.maybe_set_weights(best_hotkey)
        except Exception as exc:
            log.error("Weight setting failed: %s", exc)

    def _check_plagiarism(self, agent_source: str) -> str | None:
        """
        Compare *agent_source* against the cached best agent using AST
        structural similarity.
        """
        best_path = self._cache.agent_path

        if best_path is None or not best_path.exists():
            log.debug("No cached best agent yet — skipping plagiarism check.")
            return None

        try:
            similarity = _ast_similarity(agent_source, best_path.read_text(errors="replace"))
        except SyntaxError as exc:
            log.warning("AST parse error in submission: %s", exc)
            return None
        except Exception as exc:
            log.warning("Plagiarism check error: %s", exc)
            return None

        if similarity >= settings.plagiarism_threshold:
            return (
                f"AST similarity {similarity:.2%} ≥ threshold {settings.plagiarism_threshold:.2%}"
            )

        return None


# AST similarity helpers


def _strip_boilerplate(tree: ast.Module) -> ast.Module:
    """
    Remove boilerplate top-level nodes before similarity comparison.
    """
    meaningful: list[ast.stmt] = []

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):  # noqa: SIM102
            if node.name in ("setup", "infer"):
                continue
        if isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "__main__"
            ):
                continue
        meaningful.append(node)

    return ast.Module(body=meaningful, type_ignores=[])


def _ast_similarity(source_a: str, source_b: str) -> float:
    """
    Compute a structural similarity score in [0.0, 1.0] between two Python
    source files using AST node type bigrams.
    """
    tree_a = _strip_boilerplate(ast.parse(source_a))
    tree_b = _strip_boilerplate(ast.parse(source_b))

    nodes_a = _ast_node_bigrams(tree_a)
    nodes_b = _ast_node_bigrams(tree_b)

    if not nodes_a and not nodes_b:
        return 0.0
    if not nodes_a or not nodes_b:
        return 0.0

    set_a, set_b = set(nodes_a), set(nodes_b)
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)

    return intersection / union if union > 0 else 0.0


def _ast_node_bigrams(tree: ast.AST) -> list[tuple[str, str]]:
    """
    Walk the AST and return a list of (parent_type, child_type) bigrams.
    """
    bigrams: list[tuple[str, str]] = []

    def walk(node: ast.AST, parent_name: str = "root") -> None:
        for child in ast.iter_child_nodes(node):
            child_name = type(child).__name__
            bigrams.append((parent_name, child_name))
            walk(child, child_name)

    walk(tree)
    return bigrams
