"""
Abstractions for the delayed-evaluation (Phase 2) pipeline.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Protocol

from tensorusd.utils.backend_client import BackendClient
from tensorusd.utils.logging import get_logger
from tensorusd.validator import ground_truth as gt

log = get_logger(__name__)


@dataclass(frozen=True)
class AgentOutputRecord:
    """Immutable record representing one agent execution that produced a CSV."""

    submission_id: str
    agent_filename: str
    csv_filename: str
    csv_bytes: bytes
    upload_metadata: dict
    eval_date: str | None
    uid: int | str | None = None
    hotkey: str | None = None


@dataclass
class EvaluationResult:
    """Result of comparing an agent output CSV against ground truth."""

    submission_id: str
    agent_filename: str
    score: float
    details: dict = field(default_factory=dict)


class CsvOutputStore(Protocol):
    """Abstraction for discovering and retrieving stored agent outputs."""

    def claim_unscored_submission(self) -> AgentOutputRecord | None:
        """Claim one unscored submission and return its record, or None."""
        pass

    def download_output_csv(self, submission_id: str) -> bytes:
        """Download the output CSV for the given submission."""
        pass

    def download_ground_truth_csv(self, eval_date: str) -> bytes:
        """Return the ground-truth CSV for the given eval date (generated locally)."""
        pass

    def persist_evaluation_score(self, result: EvaluationResult) -> None:
        """Persist the computed score locally and to the backend."""
        pass


class Scorer(Protocol):
    """Pluggable scoring strategy (in-process, no Docker needed)."""

    def compute_score(
        self,
        output_csv: bytes,
        ground_truth_csv: bytes,
    ) -> float:
        """
        Compare an agent's output CSV against the ground-truth CSV.
        """
        pass


class BackendCsvOutputStore:
    """
    CsvOutputStore that talks to the TensorUSD backend for submission claims
    and output CSV downloads, but generates ground truth **locally** and
    persists results locally as well as to the backend.
    """

    def __init__(self, client: BackendClient) -> None:
        self._client = client

    def claim_unscored_submission(self) -> AgentOutputRecord | None:
        """Claim one unscored submission via the backend API."""
        data = self._client.get_unscored_submission()
        if data is None:
            return None

        submission_id: str = data.get("submission_id") or data.get("id")
        if not submission_id:
            log.error("Unscored response missing submission_id: %s", data)
            return None

        agent_filename: str = data.get("filename") or f"{submission_id}.py"
        eval_date: str | None = data.get("eval_date")
        uid: int | str | None = data.get("uid")
        hotkey: str | None = data.get("hotkey") or data.get("miner_hotkey")

        return AgentOutputRecord(
            submission_id=submission_id,
            agent_filename=agent_filename,
            csv_filename=f"agent-output-{submission_id}.csv",
            csv_bytes=b"",  # will be fetched separately
            upload_metadata=data,
            eval_date=eval_date,
            uid=uid,
            hotkey=hotkey,
        )

    def download_output_csv(self, submission_id: str) -> bytes:
        """Download output CSV via backend proxy endpoint."""
        return self._client.download_agent_output_csv(submission_id)

    def download_ground_truth_csv(self, eval_date: str) -> bytes:
        """
        Return the ground-truth CSV for *eval_date*.

        Unlike the previous implementation, this **generates the CSV locally**
        via ``ground_truth.generate_ground_truth()`` instead of downloading
        it from the backend.
        """
        return gt.generate_ground_truth(eval_date)

    def persist_evaluation_score(self, result: EvaluationResult) -> None:
        """
        Persist the computed score:
          1. Append a row to the local ``ground-truth/<eval_date>/result.csv``.
          2. Post the score to the backend.

        After this call the submission is considered scored and will not be
        re-evaluated.
        """
        eval_date = result.details.get("eval_date", "")
        uid = result.details.get("uid", "")
        hotkey = result.details.get("hotkey", "")

        # 1. Local persistence
        if eval_date and uid and hotkey:
            gt.append_result(eval_date=eval_date, uid=uid, hotkey=hotkey, score=result.score)

        # 2. Backend persistence
        self._client.post_score(result.submission_id, result.score)
        log.info(
            "Persisted score %.4f for submission %s (local + backend).",
            result.score,
            result.submission_id,
        )


class CsvComparisonScorer:
    """
    In-process scorer that compares output CSV against ground-truth CSV
    using column-wise accuracy or similarity metrics.

    This runs trusted first-party code only — no Docker sandbox required.
    """    
    def compute_score(
        self,
        output_csv: bytes,
        ground_truth_csv: bytes,
    ) -> float:
        """
        Compare output vs ground-truth CSV and return a score in [0.0, 1.0].

        The default implementation computes:
          - Column accuracy on aligned rows for target columns.
          - A row-count penalty based on deviation from ground-truth row count.
          - Overall score = column_accuracy * row_count_penalty.

        Subclasses can override this with task-specific scoring logic.
        """
        try:
            output_text = output_csv.decode("utf-8")
            gt_text = ground_truth_csv.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("CSV encoding error — scoring as 0.0")
            return 0.0

        output_rows = list(csv.DictReader(io.StringIO(output_text)))
        gt_rows = list(csv.DictReader(io.StringIO(gt_text)))

        if not output_rows or not gt_rows:
            log.warning("One or both CSVs are empty — scoring as 0.0")
            return 0.0

        # Target columns for scoring
        target_cols = {"vault_health", "tokens_minted"}
        
        # Sorting keys (in priority order)
        sort_keys = ["vault_owner", "vault_id"]
        for col in ("date", "snapshot_time_utc", "snapshot_hour"):
            if col in output_rows[0] and col in gt_rows[0]:
                sort_keys.insert(0, col)
                break

        def sort_key(row: dict) -> tuple:
            """Create a stable sort key with proper type handling."""
            key = []
            for col in sort_keys:
                val = (row.get(col) or "").strip()
                # Try numeric first for snapshot_hour, then fall back to string
                try:
                    key.append(float(val) if col == "snapshot_hour" else val)
                except (ValueError, TypeError):
                    key.append(val)
            return tuple(key)

        # Sort both lists
        output_rows = sorted(output_rows, key=sort_key)
        gt_rows = sorted(gt_rows, key=sort_key)

        # Check shared target columns
        shared_target_cols = (
            target_cols
            & set(output_rows[0].keys() if output_rows else {})
            & set(gt_rows[0].keys() if gt_rows else {})
        )
        
        if not shared_target_cols:
            log.warning("None of the required columns (vault_health, tokens_minted) found — scoring as 0.0")
            return 0.0

        output_count = len(output_rows)
        gt_count = len(gt_rows)

        # Align by row index after sorting
        n = min(output_count, gt_count)
        if n == 0:
            return 0.0

        column_scores: dict[str, float] = {}

        for col in shared_target_cols:
            correct = 0
            for i in range(n):
                out_val = output_rows[i].get(col, "").strip()
                gt_val = gt_rows[i].get(col, "").strip()

                # Numeric comparison with tolerance
                try:
                    out_num = float(out_val)
                    gt_num = float(gt_val)
                    if abs(out_num - gt_num) < 1e-6:
                        correct += 1
                except (ValueError, TypeError):
                    # Exact string match
                    if out_val == gt_val:
                        correct += 1

            column_scores[col] = correct / n if n > 0 else 0.0

        # Average accuracy across scored target columns.
        base_accuracy = sum(column_scores.values()) / len(column_scores) if column_scores else 0.0

        # Penalize missing/extra rows relative to ground truth.
        # diff_ratio = 0.0 when counts match, 0.99 when only 1/100 rows are present, etc.
        diff_ratio = abs(output_count - gt_count) / gt_count
        row_count_penalty = max(0.0, 1.0 - diff_ratio)
        overall = base_accuracy * row_count_penalty

        log.debug(
            (
                "Scored %d/%d rows (output=%d) on %s after sorting by %s — "
                "base=%.4f penalty=%.4f overall=%.4f"
            ),
            n,
            gt_count,
            output_count,
            sorted(shared_target_cols),
            sort_keys,
            base_accuracy,
            row_count_penalty,
            overall,
        )
        return min(max(overall, 0.0), 1.0)


class DelayedEvaluator:
    """
    Orchestrates the delayed-evaluation (Phase 2) scoring cycle.

    Usage:
        evaluator = DelayedEvaluator(client=backend_client)
        result = evaluator.run_once()
        if result:
            print(f"Scored {result.submission_id} = {result.score}")
    """

    def __init__(
        self,
        client: BackendClient,
        store: CsvOutputStore | None = None,
        scorer: Scorer | None = None,
    ) -> None:
        self._store = store or BackendCsvOutputStore(client)
        self._scorer = scorer or CsvComparisonScorer()

    @property
    def store(self) -> CsvOutputStore:
        """Expose the store for testing/debugging."""
        return self._store

    @property
    def scorer(self) -> Scorer:
        """Expose the scorer for testing/debugging."""
        return self._scorer

    def run_once(self) -> EvaluationResult | None:
        """
        Execute one scoring cycle:
          1. Claim an unscored submission from the backend.
          2. Check whether it has already been scored locally (by uid + hotkey).
          3. Download the output CSV from object storage.
          4. Get the ground-truth CSV (generated locally).
          5. Compute the score.
          6. Persist the score locally and to the backend.

        Returns the EvaluationResult if a submission was scored, or None
        if no unscored submissions are ready yet.
        """
        # Step 1: Claim
        record = self._store.claim_unscored_submission()
        if record is None:
            log.debug("No unscored submissions ready.")
            return None

        log.info(
            "Claimed unscored submission %s (eval_date=%s hotkey=%s)",
            record.submission_id,
            record.eval_date or "?",
            record.hotkey or "?",
        )

        eval_date = record.eval_date

        # Step 2: Check if already scored locally (before downloading anything)
        if eval_date and record.uid is not None and record.hotkey:
            if gt.is_already_scored(eval_date, record.uid, record.hotkey):
                log.info(
                    "Submission %s (uid=%s hotkey=%s) already scored locally for %s — skipping.",
                    record.submission_id,
                    record.uid,
                    record.hotkey,
                    eval_date,
                )
                # Still mark it scored on the backend so we don't claim it again
                # (use score from local file = re-read it; for simplicity just post 0)
                # Better: we could skip posting altogether and let the backend move on.
                # But posting 0 would penalize — so instead log and return None.
                return None

        # Step 3: Download output CSV
        try:
            output_csv_bytes = self._store.download_output_csv(record.submission_id)
        except Exception as exc:
            log.error(
                "Failed to download output CSV for %s: %s",
                record.submission_id,
                exc,
            )
            return None

        if not output_csv_bytes or not output_csv_bytes.strip():
            log.warning(
                "Output CSV for %s is empty — skipping score (will retry when ground truth is available).",
                record.submission_id,
            )
            return None

        # Step 4: Get ground truth (generated locally)
        if not eval_date:
            log.warning(
                "No eval_date for %s — skipping score (submission not yet matured).",
                record.submission_id,
            )
            return None

        try:
            gt_csv_bytes = self._store.download_ground_truth_csv(eval_date)
        except Exception as exc:
            log.error(
                "Failed to get ground-truth CSV for eval_date=%s (%s): %s",
                eval_date,
                record.submission_id,
                exc,
            )
            return None

        if not gt_csv_bytes or not gt_csv_bytes.strip():
            log.warning(
                "Ground-truth CSV for eval_date=%s is empty (first run or GT not yet collected) — skipping %s",
                eval_date,
                record.submission_id,
            )
            return None

        # Step 5: Compute score
        log.info("Computing score for submission %s ...", record.submission_id)
        try:
            score = self._scorer.compute_score(
                output_csv=output_csv_bytes,
                ground_truth_csv=gt_csv_bytes,
            )
        except Exception as exc:
            log.error(
                "Scoring failed for %s: %s — marking as 0.0",
                record.submission_id,
                exc,
            )
            score = 0.0

        result = EvaluationResult(
            submission_id=record.submission_id,
            agent_filename=record.agent_filename,
            score=score,
            details={
                "eval_date": eval_date,
                "uid": str(record.uid or ""),
                "hotkey": record.hotkey or "",
                "output_rows": len(output_csv_bytes),
                "ground_truth_rows": len(gt_csv_bytes),
            },
        )

        # Step 6: Persist (local + backend)
        try:
            self._store.persist_evaluation_score(result)
            log.info(
                "Successfully scored submission %s = %.4f (uid=%s hotkey=%s)",
                record.submission_id,
                score,
                record.uid or "?",
                record.hotkey or "?",
            )
        except Exception as exc:
            log.error(
                "Failed to persist score for %s: %s",
                record.submission_id,
                exc,
            )
            return None

        return result

    def backfill(self, submission_ids: list[str]) -> list[EvaluationResult]:
        """
        Backfill scores for specific submissions (manual or cron-triggered).

        Args:
            submission_ids: List of submission IDs to re-evaluate.

        Returns:
            List of EvaluationResult objects.
        """
        results: list[EvaluationResult] = []
        for sid in submission_ids:
            log.warning("Backfill for %s not yet implemented.", sid)
        return results