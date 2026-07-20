"""
Main validator evaluation loop.
"""

from __future__ import annotations

import ast
import io
import csv
import threading

import bittensor as bt
import requests

from tensorusd.utils.logging import get_logger
from tensorusd.utils.backend_client import BackendClient
from tensorusd.utils.agent_cache import BestAgentCache, BestAgentWatcher
from tensorusd.utils.sandbox import SandboxRunner
from tensorusd.utils.scored_cache import ScoredCache
from tensorusd.utils.security import validate_agent_file, validate_agent_format
from tensorusd.validator.delayed_evaluation import (
    AgentOutputRecord,
    BackendCsvOutputStore,
    CsvComparisonScorer,
    DelayedEvaluator,
)
from tensorusd.validator.ground_truth_collector import GroundTruthCollector
from tensorusd.utils.pricing import get_most_expensive_allowed_model


log = get_logger(__name__)


class ValidatorCore:
    """
    Orchestrates the agent-evaluation pipeline (Phase 1 sandbox execution +
    Phase 2 delayed scoring).

      - poll the backend for submissions
      - sandbox / validate / score them
      - keep BestAgentCache up to date (via the background BestAgentWatcher)

    Weight-setting now lives entirely in forward_mech1.
    """

    def __init__(
        self,
        wallet: bt.Wallet,
        netuid: int,
        network: str,
        rpc_endpoint: str,
        scored_cache_path: str,
        scored_cache_max_size: int,
        validator_poll_interval: int,
        scoring_poll_interval: int,
        plagiarism_threshold: int,
        vault_address: str,
        vault_metadata_path: str,
    ) -> None:

        self.wallet = wallet
        self.netuid = netuid
        self.network = network
        self.rpc_endpoint = rpc_endpoint
        self.scored_cache_path = scored_cache_path
        self.scored_cache_max_size = scored_cache_max_size
        self.validator_poll_interval = validator_poll_interval
        self.scoring_poll_interval = scoring_poll_interval
        self.plagiarism_threshold = plagiarism_threshold

        self.vault_address = vault_address
        self.vault_metadata_path = vault_metadata_path
        self._client = BackendClient(wallet)
        self._cache = BestAgentCache()
        self._watcher = BestAgentWatcher(self._client, self._cache)
        self._sandbox = SandboxRunner()

        # Phase 2 delayed evaluation
        self._delayed_evaluator = DelayedEvaluator(
            client=self._client,
            store=BackendCsvOutputStore(self._client),
            scorer=CsvComparisonScorer(),
        )

        # Survives validator restarts; auto-evicts oldest entries at max_size.
        self._scored = ScoredCache(
            path=self.scored_cache_path,
            max_size=self.scored_cache_max_size,
        )

        # Ground-truth collector (background daemon — collects every hour)
        self._gt_collector = GroundTruthCollector(
            self.wallet,
            self.network,
            self.rpc_endpoint,
            self.vault_address,
            self.vault_metadata_path,
        )

        self._eval_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # Lifecycle

    def start_background(self) -> None:
        """
        Start all background daemons (watcher, ground-truth collector, and the
        evaluation loop itself) and return immediately. Call this from
        `AgentValidator.setup()`. Does NOT block — `forward()` / the base
        neuron's own `run()` loop owns blocking + weight-setting.
        """
        log.info(
            "ValidatorCore starting background services:\n"
            "%s  Network : %s\n"
            "%s  NetUID  : %d\n"
            "%s  Hotkey  : %s",
            "\t" * 6,
            self.network,
            "\t" * 6,
            self.netuid,
            "\t" * 6,
            self.wallet.hotkey.ss58_address,
        )
        self._watcher.start()
        self._gt_collector.start()

        self._stop_event.clear()
        self._eval_thread = threading.Thread(
            target=self._run_loop,
            name="validator-core-eval-loop",
            daemon=True,
        )
        self._eval_thread.start()

    def stop(self) -> None:
        """Signal the background evaluation loop to stop and tear down daemons."""
        self._stop_event.set()
        if self._eval_thread is not None:
            self._eval_thread.join(timeout=10)
        self._gt_collector.stop()
        self._watcher.stop()
        self._client.close()
        log.info("ValidatorCore stopped.")

    # Public accessors (read by forward_mech1)

    def get_best_hotkey(self) -> str | None:
        """
        Return the current best-known miner hotkey, preferring the local
        cache (kept warm by `BestAgentWatcher`) and falling back to a direct
        backend call if the cache is empty.
        """
        best_meta = self._cache.best_meta
        if best_meta and best_meta.hotkey:
            return best_meta.hotkey

        try:
            meta = self._client.get_best_submission_meta()
        except Exception as exc:
            log.error("get_best_submission_meta failed: %s", exc)
            return None

        if meta:
            return meta.get("hotkey") or meta.get("miner_hotkey")
        return None

    # Main loop (background thread only — no weight-setting here)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                winner_hk, _ = self._evaluation_cycle()
                if not winner_hk:
                    self._run_scoring_cycle()
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 403:
                    log.error(
                        "Received 403 from backend — this hotkey has no validator permit.\n"
                        "  Shutting down eval loop. Run with a registered validator hotkey."
                    )
                    return
                log.error(
                    "Unexpected HTTP error in evaluation cycle: %s", exc, exc_info=True
                )
                self._stop_event.wait(self.validator_poll_interval)
            except Exception as exc:
                # Log but never crash — always keep running
                log.error(
                    "Unexpected error in evaluation cycle: %s", exc, exc_info=True
                )
                self._stop_event.wait(self.validator_poll_interval)

    def _run_scoring_cycle(self) -> None:
        """
        Phase 2: attempt to claim and score a matured unscored submission.
        """
        try:
            result = self._delayed_evaluator.run_once()
            if result is not None:
                log.info(
                    "Phase 2 scored submission %s = %.4f",
                    result.submission_id,
                    result.score,
                )
            else:
                log.debug(
                    "No unscored submissions ready yet — sleeping %ds.",
                    self.scoring_poll_interval,
                )
                self._stop_event.wait(self.scoring_poll_interval)
        except Exception as exc:
            log.error("Scoring cycle failed: %s", exc, exc_info=True)
            self._stop_event.wait(self.scoring_poll_interval)

    def _evaluation_cycle(self):
        """One full pass: poll → validate → sandbox → upload. No weight-setting."""

        # Poll for next submission
        submission = self._client.get_unevaluated_submission()

        if submission is None:
            log.info(
                "No unevaluated submissions.  Sleeping %ds.",
                self.validator_poll_interval,
            )
            self._stop_event.wait(self.validator_poll_interval)
            return "", 1

        log.debug("Raw submission response: %s", submission)
        sub_id: str = submission.get("submission_id") or submission.get("id")
        if not sub_id:
            log.error(
                "Submission response missing id field. Keys received: %s",
                list(submission.keys()),
            )
            return "", 1

        # Check persistent scored cache — survives restarts
        if sub_id in self._scored:
            log.warning(
                "Submission %s already scored by this validator — skipping.",
                sub_id,
            )
            return "", 1

        miner_hotkey: str | None = (
            submission.get("miner_hotkey") or submission.get("hotkey") or None
        )
        model_id: str | None = submission.get("model_id") or None
        est_input_tokens: int = int(submission.get("est_input_tokens") or 0)
        est_output_tokens: int = int(submission.get("est_output_tokens") or 0)
        budget_usd: float = float(submission.get("budget_usd") or 0.0)
        run_budget_usd: float = budget_usd / 3.0 if budget_usd > 0 else 0.0
        log.info(
            "Evaluating submission %s from %s (model=%s, est_tokens=%d/%d, budget_usd=%.6f, run_budget_usd=%.6f)",
            sub_id,
            miner_hotkey or "unknown",
            model_id or "unknown",
            est_input_tokens,
            est_output_tokens,
            budget_usd,
            run_budget_usd,
        )

        token_budget = est_input_tokens + est_output_tokens
        if token_budget <= 0 and budget_usd > 0:
            try:
                pricing = get_most_expensive_allowed_model(self._sandbox.allowed_models)
                token_budget = int(
                    (
                        (run_budget_usd * 1_000_000.0)
                        / max(pricing.output_usd_per_million_tokens, 1e-9)
                    )
                )
            except Exception as exc:
                log.warning("Could not derive token budget for %s: %s", sub_id, exc)
                token_budget = None
        if token_budget is not None and token_budget <= 0:
            token_budget = None

        agent_filename: str = (
            submission.get("agent_filename")
            or submission.get("filename")
            or f"{sub_id}.py"
        )

        # Download agent file
        try:
            agent_bytes = self._client.download_submission_file(sub_id)
        except Exception as exc:
            log.error("Failed to download submission %s: %s", sub_id, exc)
            self._upload_fallback_empty_csv_and_record(
                sub_id, agent_filename, miner_hotkey
            )
            return "", 1

        # Security validation — pure Python, binary check, AST scan, token scan.
        security_reason = validate_agent_file(agent_bytes)
        if security_reason:
            log.warning("Security check failed for %s: %s", sub_id, security_reason)
            try:
                self._client.blacklist_miner(
                    miner_hotkey, sub_id, f"security: {security_reason}"
                )
            except Exception as bl_exc:
                log.error("Blacklist call failed (continuing): %s", bl_exc)
            self._upload_fallback_empty_csv_and_record(
                sub_id, agent_filename, miner_hotkey
            )
            self._scored.add(sub_id)
            return "", 1

        format_reason = validate_agent_format(agent_bytes)
        if format_reason:
            log.warning("Format check failed for %s: %s", sub_id, format_reason)
            self._upload_fallback_empty_csv_and_record(
                sub_id, agent_filename, miner_hotkey
            )
            self._scored.add(sub_id)
            return "", 1

        # Plagiarism check
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
            self._upload_fallback_empty_csv_and_record(
                sub_id, agent_filename, miner_hotkey
            )
            self._scored.add(sub_id)
            return "", 1

        # Sandbox evaluation
        log.info("Running sandbox for submission %s", sub_id)
        sandbox_result = self._sandbox.run(
            agent_bytes,
            model_id=model_id,
            token_budget=token_budget,
            cost_budget_usd=run_budget_usd if run_budget_usd > 0 else None,
        )

        if not sandbox_result.success or sandbox_result.output_csv_bytes is None:
            log.warning(
                "Sandbox failed for %s (exit=%s, sandbox_log=%s): %s",
                sub_id,
                sandbox_result.exit_code,
                sandbox_result.log_path,
                sandbox_result.stderr[:300],
            )
            self._upload_fallback_empty_csv_and_record(
                sub_id, agent_filename, miner_hotkey
            )
            self._scored.add(sub_id)
            return "", 1

        output_csv_bytes = sandbox_result.output_csv_bytes

        # Validate the output CSV is non-empty and parseable.
        if not self._validate_csv(output_csv_bytes):
            log.warning(
                "Invalid or empty CSV output for %s — uploading as-is for delayed scoring.",
                sub_id,
            )
            self._upload_fallback_empty_csv_and_record(
                sub_id, agent_filename, miner_hotkey
            )
            self._scored.add(sub_id)
            return "", 1

        # Upload the generated CSV to backend storage
        try:
            upload_meta = self._client.upload_agent_output(
                csv_bytes=output_csv_bytes,
                agent_filename=agent_filename,
                submission_id=sub_id,
            )
            log.info(
                "Output CSV uploaded for %s — file_id=%s  eval_date=%s",
                sub_id,
                upload_meta.get("file_id", "?"),
                upload_meta.get("eval_date", "?"),
            )
        except Exception as exc:
            log.error(
                "Failed to upload output CSV for %s: %s — marking as failed.",
                sub_id,
                exc,
            )
            self._upload_fallback_empty_csv_and_record(
                sub_id, agent_filename, miner_hotkey
            )
            self._scored.add(sub_id)
            return "", 1

        # Record evaluation metadata for future delayed-scoring stage
        self._record_evaluation(
            submission_id=sub_id,
            agent_filename=agent_filename,
            csv_bytes=output_csv_bytes,
            upload_metadata=upload_meta,
        )

        # Phase 1 complete — tell the backend the submission is evaluated
        try:
            self._client.mark_submission_evaluated(sub_id)
        except Exception as exc:
            log.error("Failed to mark submission %s as evaluated: %s", sub_id, exc)
            # Non-fatal — backend will retry via polling or timeout recovery

        log.info(
            "Phase 1 complete for submission %s — output CSV uploaded, awaiting delayed scoring.",
            sub_id,
        )

        return (miner_hotkey, 1)

    # Helpers

    def _validate_csv(self, csv_bytes: bytes) -> bool:
        """
        Verify that *csv_bytes* contains parseable CSV data with at least one row
        (beyond headers).
        """
        try:
            text = csv_bytes.decode("utf-8")
            if not text.strip():
                log.warning("CSV content is empty (whitespace only).")
                return False

            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)

            if not reader.fieldnames or len(reader.fieldnames) < 1:
                log.warning("CSV has no column headers.")
                return False

            if len(rows) < 1:
                log.warning("CSV has headers but no data rows.")
                return False

            log.debug(
                "CSV validation passed: %d rows, %d columns.",
                len(rows),
                len(reader.fieldnames),
            )
            return True

        except csv.Error as exc:
            log.warning("CSV parse error: %s", exc)
            return False
        except UnicodeDecodeError as exc:
            log.warning("CSV encoding error (not valid UTF-8): %s", exc)
            return False
        except Exception as exc:
            log.warning("Unexpected CSV validation error: %s", exc)
            return False

    def _record_evaluation(
        self,
        submission_id: str,
        agent_filename: str,
        csv_bytes: bytes,
        upload_metadata: dict,
    ) -> None:
        """
        Persist an AgentOutputRecord for the future delayed-evaluation stage.
        """
        eval_date = upload_metadata.get("eval_date")

        record = AgentOutputRecord(
            submission_id=submission_id,
            agent_filename=agent_filename,
            csv_filename=f"agent-output-{submission_id}.csv",
            csv_bytes=csv_bytes,
            upload_metadata=upload_metadata,
            eval_date=eval_date,
        )

        log.info(
            "Evaluation record — sub=%s  agent=%s  eval_date=%s  file_id=%s",
            record.submission_id,
            record.agent_filename,
            record.eval_date or "?",
            record.upload_metadata.get("file_id", "?"),
        )

    def _upload_fallback_empty_csv_and_record(
        self,
        submission_id: str,
        agent_filename: str,
        miner_hotkey: str | None,
    ) -> None:
        """
        Upload a headers-only empty CSV when the agent cannot produce output,
        so the submission enters the Phase 2 scoring pipeline instead of being
        immediately zero-scored.
        """
        empty_csv = b"snapshot_hour,snapshot_time_utc,block_number,vault_owner,vault_id,vault_health,tokens_minted\n"
        try:
            upload_meta = self._client.upload_agent_output(
                csv_bytes=empty_csv,
                agent_filename=agent_filename,
                submission_id=submission_id,
            )
            log.info(
                "Uploaded fallback empty CSV for %s — file_id=%s  eval_date=%s",
                submission_id,
                upload_meta.get("file_id", "?"),
                upload_meta.get("eval_date", "?"),
            )
            self._record_evaluation(
                submission_id=submission_id,
                agent_filename=agent_filename,
                csv_bytes=empty_csv,
                upload_metadata=upload_meta,
            )
            try:
                self._client.mark_submission_evaluated(submission_id)
            except Exception as mark_exc:
                log.error(
                    "Failed to mark fallback submission %s as evaluated: %s",
                    submission_id,
                    mark_exc,
                )
        except Exception as exc:
            log.error(
                "Failed to upload fallback empty CSV for %s: %s",
                submission_id,
                exc,
            )

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
            similarity = _ast_similarity(
                agent_source, best_path.read_text(errors="replace")
            )
        except SyntaxError as exc:
            log.warning("AST parse error in submission: %s", exc)
            return None
        except Exception as exc:
            log.warning("Plagiarism check error: %s", exc)
            return None

        if similarity >= self.plagiarism_threshold:
            return f"AST similarity {similarity:.2%} ≥ threshold {self.plagiarism_threshold:.2%}"

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
            if node.name == "setup":
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
