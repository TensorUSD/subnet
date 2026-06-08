"""
Abstractions for the delayed-evaluation (Phase 2) pipeline.
"""

from __future__ import annotations

import io
import csv
from dataclasses import dataclass, field
from typing import Protocol

from tensorusd.auth.config import settings
from tensorusd.utils.backend_client import BackendClient
from tensorusd.utils.logging import get_logger

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
        """Download the ground-truth CSV for the given eval date."""
        pass

    def persist_evaluation_score(self, result: EvaluationResult) -> None:
        """Persist the computed score back to the backend."""
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
    Real CsvOutputStore that talks to the TensorUSD backend.

    All S3/MinIO access is proxied through the backend — no direct S3
    credentials are needed on the validator.
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

        return AgentOutputRecord(
            submission_id=submission_id,
            agent_filename=agent_filename,
            csv_filename=f"agent-output-{submission_id}.csv",
            csv_bytes=b"",  # will be fetched separately
            upload_metadata=data,
            eval_date=eval_date,
        )

    def download_output_csv(self, submission_id: str) -> bytes:
        """Download output CSV via backend proxy endpoint."""
        return self._client.download_agent_output_csv(submission_id)

    def download_ground_truth_csv(self, eval_date: str) -> bytes:
        """Download ground-truth CSV via backend proxy endpoint."""
        return self._client.download_ground_truth(eval_date=eval_date)

    def persist_evaluation_score(self, result: EvaluationResult) -> None:
        """Post the computed score to the backend."""
        self._client.post_score(result.submission_id, result.score)
        log.info(
            "Persisted score %.4f for submission %s",
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

        The default implementation computes mean column-wise accuracy:
          - For numeric columns: 1 - mean_absolute_error / (max - min)
          - For categorical/text columns: exact match accuracy
          - Overall score = weighted average across all columns

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

        # Align by row index (assumes same ordering)
        n = min(len(output_rows), len(gt_rows))
        if n == 0:
            return 0.0

        # Determine shared columns
        shared_cols = set(output_rows[0].keys()) & set(gt_rows[0].keys())
        if not shared_cols:
            log.warning("No shared columns between output and ground-truth CSV — scoring as 0.0")
            return 0.0

        column_scores: dict[str, float] = {}

        for col in shared_cols:
            correct = 0
            for i in range(n):
                out_val = output_rows[i].get(col, "").strip()
                gt_val = gt_rows[i].get(col, "").strip()

                # Attempt numeric comparison
                try:
                    out_num = float(out_val)
                    gt_num = float(gt_val)
                    # Allow small tolerance for floating-point
                    if abs(out_num - gt_num) < 1e-6:
                        correct += 1
                except (ValueError, TypeError):
                    # Fall back to exact string match
                    if out_val == gt_val:
                        correct += 1

            column_scores[col] = correct / n if n > 0 else 0.0

        # Overall score = mean of column scores
        overall = sum(column_scores.values()) / len(column_scores) if column_scores else 0.0

        log.debug(
            "Scored %d rows across %d shared columns — overall=%.4f",
            n,
            len(shared_cols),
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
          2. Download its output CSV.
          3. Download the corresponding ground-truth CSV.
          4. Compute the score.
          5. Persist the score back to the backend.

        Returns the EvaluationResult if a submission was scored, or None
        if no unscored submissions are ready yet.
        """
        # Step 1: Claim
        record = self._store.claim_unscored_submission()
        if record is None:
            log.debug("No unscored submissions ready.")
            return None

        log.info(
            "Claimed unscored submission %s (eval_date=%s)",
            record.submission_id,
            record.eval_date or "?",
        )

        # Step 2: Download output CSV
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
            log.warning("Output CSV for %s is empty — scoring as 0.0", record.submission_id)
            # Still persist a score of 0 so the submission is marked scored
            result = EvaluationResult(
                submission_id=record.submission_id,
                agent_filename=record.agent_filename,
                score=0.0,
                details={"error": "empty output CSV"},
            )
            self._store.persist_evaluation_score(result)
            return result

        # Step 3: Download ground truth
        eval_date = record.eval_date
        if not eval_date:
            log.warning("No eval_date for %s — scoring as 0.0", record.submission_id)
            result = EvaluationResult(
                submission_id=record.submission_id,
                agent_filename=record.agent_filename,
                score=0.0,
                details={"error": "missing eval_date"},
            )
            self._store.persist_evaluation_score(result)
            return result

        try:
            gt_csv_bytes = self._store.download_ground_truth_csv(eval_date)
        except Exception as exc:
            log.error(
                "Failed to download ground-truth CSV for eval_date=%s (%s): %s",
                eval_date,
                record.submission_id,
                exc,
            )
            # Do not persist — ground truth may not be available yet, retry later
            return None

        if not gt_csv_bytes or not gt_csv_bytes.strip():
            log.warning(
                "Ground-truth CSV for eval_date=%s is empty — skipping %s",
                eval_date,
                record.submission_id,
            )
            return None

        # Step 4: Compute score
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
                "output_rows": len(output_csv_bytes),
                "ground_truth_rows": len(gt_csv_bytes),
            },
        )

        # Step 5: Persist
        try:
            self._store.persist_evaluation_score(result)
            log.info(
                "Successfully scored submission %s = %.4f",
                record.submission_id,
                score,
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
            # For backfill, we need to reconstruct the record from the backend
            # Currently this is a stub — implement if needed.
            log.warning("Backfill for %s not yet implemented.", sid)
        return results