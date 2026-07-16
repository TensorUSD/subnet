"""
Main validator evaluation loop.
"""

from __future__ import annotations

import ast
import io
import csv
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
from tensorusd.utils.security import validate_agent_file, validate_agent_format
from tensorusd.utils.weight_setter import WeightSetter
from tensorusd.validator.delayed_evaluation import (
    AgentOutputRecord,
    BackendCsvOutputStore,
    CsvComparisonScorer,
    DelayedEvaluator,
)
from tensorusd.validator.ground_truth_collector import GroundTruthCollector
from tensorusd.utils.pricing import get_most_expensive_allowed_model
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neurons.validator import Validator

log = get_logger(__name__)


class ValidatorCore:
    """
    Orchestrates the full validator evaluation loop.
    """

    def __init__(self, wallet: bt.Wallet, validator: "Validator",mechid: int = 1) -> None:
        self._wallet = wallet
        self._mechid = mechid
        self._validator = validator

        # Bittensor chain connections
        self._subtensor = validator.subtensor
        self._metagraph = validator.metagraph
        # Core components
        self._client = BackendClient(wallet)
        self._cache = BestAgentCache()
        self._watcher = BestAgentWatcher(self._client, self._cache)
        self._sandbox = SandboxRunner()
        self._weight_setter = WeightSetter(
            self._wallet, self._subtensor, self._metagraph
        )

        # Phase 2 delayed evaluation
        self._delayed_evaluator = DelayedEvaluator(
            client=self._client,
            store=BackendCsvOutputStore(self._client),
            scorer=CsvComparisonScorer(),
        )

        # Persistent, bounded cache of already-scored submission IDs.
        # Survives validator restarts; auto-evicts oldest entries at max_size.
        self._scored = ScoredCache(
            path=settings.scored_cache_path,
            max_size=settings.scored_cache_max_size,
        )

        # Ground-truth collector (background daemon — collects 24 hourly
        # snapshots after 23:00 UTC each day for the next day's scoring).
        self._gt_collector = GroundTruthCollector(self._wallet)

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
        self._gt_collector.start()

        try:
            self._run_loop()
        except KeyboardInterrupt:
            log.info("Received keyboard interrupt, shutting down.")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        self._gt_collector.stop()
        self._watcher.stop()
        self._weight_setter.stop_monitor()
        self._client.close()
        log.info("Validator stopped.")

    # Main loop

    def _run_loop(self) -> None:
        while True:
            try:
                # Phase 1: Agent execution
                winner_hk,weight = self._evaluation_cycle()

                if not winner_hk:
                    self._maybe_set_weights(self._wallet.hotkey.ss58_address)
                    self._run_scoring_cycle()
                else:
                    self._maybe_set_weights(winner_hk)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 403:
                    log.error(
                        "Received 403 from backend — this hotkey has no validator permit.\n"
                        "  Shutting down. Run with a registered validator hotkey."
                    )
                    raise SystemExit(1)  # noqa: B904
                log.error(
                    "Unexpected HTTP error in evaluation cycle: %s", exc, exc_info=True
                )
                time.sleep(settings.validator_poll_interval)
            except Exception as exc:
                # Log but never crash — always keep running
                log.error(
                    "Unexpected error in evaluation cycle: %s", exc, exc_info=True
                )
                time.sleep(settings.validator_poll_interval)

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
                    settings.scoring_poll_interval,
                )
                time.sleep(settings.scoring_poll_interval)
        except Exception as exc:
            log.error("Scoring cycle failed: %s", exc, exc_info=True)
            time.sleep(settings.scoring_poll_interval)

    def _evaluation_cycle(self) -> None:
        """One full pass: poll → validate → sandbox → upload → (maybe) set weights."""

        # Poll for next submission
        submission = self._client.get_unevaluated_submission()

        if submission is None:
            log.info(
                "No unevaluated submissions.  Sleeping %ds.",
                settings.validator_poll_interval,
            )

            return "",1

        log.debug("Raw submission response: %s", submission)
        sub_id: str = submission.get("submission_id") or submission.get("id")
        if not sub_id:
            log.error(
                "Submission response missing id field. Keys received: %s",
                list(submission.keys()),
            )
            return "",1

        # Check persistent scored cache — survives restarts
        if sub_id in self._scored:
            log.warning(
                "Submission %s already scored by this validator — skipping.",
                sub_id,
            )
            return "",1
            # time.sleep(settings.validator_poll_interval) #TODO:: keep it at the end
            # return True

        miner_hotkey: str | None = (
            submission.get("miner_hotkey") or submission.get("hotkey") or None
        )
        model_id: str | None = submission.get("model_id") or None
        est_input_tokens: int = int(submission.get("est_input_tokens") or 0)
        est_output_tokens: int = int(submission.get("est_output_tokens") or 0)
        budget_usd: float = float(submission.get("budget_usd") or 0.0)
        run_budget_usd: float = budget_usd / 2.0 if budget_usd > 0 else 0.0
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
                    ((run_budget_usd * 1_000_000.0) / max(pricing.output_usd_per_million_tokens, 1e-9))
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
            return "",1
        

        # Security validation — pure Python, binary check, AST scan, token scan.
        # This replaces the old regex-based _detect_malware().
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
            return "",1
           

        format_reason = validate_agent_format(agent_bytes)
        if format_reason:
            log.warning("Format check failed for %s: %s", sub_id, format_reason)
            self._upload_fallback_empty_csv_and_record(
                sub_id, agent_filename, miner_hotkey
            )
            self._scored.add(sub_id)
            return "",1
           

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
            # Upload empty CSV so Phase 2 can still score after 7 days
            self._upload_fallback_empty_csv_and_record(
                sub_id, agent_filename, miner_hotkey
            )
            self._scored.add(sub_id)
            # self._maybe_set_weights()
            return "",1

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
            return "",1

        output_csv_bytes = sandbox_result.output_csv_bytes

        # Validate the output CSV is non-empty and parseable.
        # Even if invalid, we upload it as-is so Phase 2 can evaluate.
        if not self._validate_csv(output_csv_bytes):
            log.warning(
                "Invalid or empty CSV output for %s — uploading as-is for delayed scoring.",
                sub_id,
            )
            # Upload as-is (even if empty/invalid) — Phase 2 will score it
            self._upload_fallback_empty_csv_and_record(
                sub_id, agent_filename, miner_hotkey
            )
            self._scored.add(sub_id)
            self._maybe_set_weights()
            return True

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
            # Upload empty CSV on upload failure so Phase 2 can still score
            self._upload_fallback_empty_csv_and_record(
                sub_id, agent_filename, miner_hotkey
            )
            self._scored.add(sub_id)
            return "",1

        # Record evaluation metadata for future delayed-scoring stage
        self._record_evaluation(
            submission_id=sub_id,
            agent_filename=agent_filename,
            csv_bytes=output_csv_bytes,
            upload_metadata=upload_meta,
        )

        # Phase 1 complete — tell the backend the submission is evaluated
        # so Phase 2 can pick it up after the maturation window.
        try:
            self._client.mark_submission_evaluated(sub_id)
        except Exception as exc:
            log.error("Failed to mark submission %s as evaluated: %s", sub_id, exc)
            # Non-fatal — backend will retry via polling or timeout recovery

        # Phase 1 complete — submission is now evaluated; no score is posted.
        # The real score will be computed later in Phase 2 (delayed evaluation)
        # once ground truth is available and the maturation window has passed.
        log.info(
            "Phase 1 complete for submission %s — output CSV uploaded, awaiting delayed scoring.",
            sub_id,
        )

    
        return (miner_hotkey,1) 
    

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
            log.info("No best hotkey found — falling back to cold-start burn (UID 0).")

        try:
            self._weight_setter.maybe_set_weights(best_hotkey)
        except Exception as exc:
            log.error("Weight setting failed: %s", exc)

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

        Currently this logs the record and constructs an AgentOutputRecord.
        In the future, this record could be written to a local store (SQLite,
        JSON lines file, etc.) that the DelayedEvaluator reads to discover
        matured outputs.

        The *upload_metadata* dict returned from the backend contains at least:
            file_id, file_path, filename, eval_date
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

        # Future: persist `record` to a local store that the DelayedEvaluator
        # can query. For example, append to a JSON-lines file:
        #
        #   import json
        #   store_path = Path(".TENSORUSD_cache/evaluations.jsonl")
        #   with open(store_path, "a") as f:
        #       f.write(json.dumps({
        #           "submission_id": record.submission_id,
        #           "agent_filename": record.agent_filename,
        #           "eval_date": record.eval_date,
        #           "upload_metadata": record.upload_metadata,
        #       }) + "\n")
        #

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
            # Mark evaluated so Phase 2 can pick it up
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

        if similarity >= settings.plagiarism_threshold:
            return f"AST similarity {similarity:.2%} ≥ threshold {settings.plagiarism_threshold:.2%}"

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