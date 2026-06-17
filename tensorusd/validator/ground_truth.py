"""
Ground-truth CSV generation for Phase 2 scoring.

Ground-truth data is generated from on-chain vault snapshots and
stored at::

    ground-truth/<eval_date>/ground-truth.csv

Each row contains:
    block, vault_health, tokens_minted

If a real ground-truth file exists (built by build_ground_truth.py), it is
returned as-is. Otherwise synthetic placeholder data is generated.
"""

from __future__ import annotations

import csv
import io
import os
import random
from pathlib import Path

from tensorusd.auth.config import settings
from tensorusd.utils.logging import get_logger

log = get_logger(__name__)


def generate_ground_truth(eval_date: str) -> bytes:
    """
    Generate a ground-truth CSV for the given *eval_date* (ISO-8601 date string).

    The generated CSV contains three columns:
        block, vault_health, tokens_minted

    If a ground-truth file already exists at
    ``ground-truth/<eval_date>/ground-truth.csv``, it is returned as-is.

    This is a **placeholder** implementation. It generates synthetic data.
    Replace the inner logic with real on-chain vault queries for production.
    """
    gt_dir = settings.ground_truth_dir / eval_date
    gt_path = gt_dir / "ground-truth.csv"

    # Return existing file if present
    if gt_path.exists():
        log.info("Ground-truth CSV already exists at %s — reusing.", gt_path)
        return gt_path.read_bytes()

    # Create directory
    gt_dir.mkdir(parents=True, exist_ok=True)

    # ---- Placeholder: generate synthetic data ----
    # In production this would query on-chain vault snapshots for the given date.
    # The generation follows the expected schema: block, vault_health, tokens_minted.
    #
    # For the placeholder, we produce a small set of realistic-looking rows.
    num_rows = _get_num_vaults_for_date(eval_date)
    rows: list[dict[str, str | float | int]] = []

    for i in range(num_rows):
        block = 1_000_000 + i * 100  # dummy block numbers
        # vault_health is a ratio between 0.8 and 2.5
        vault_health = round(random.uniform(0.8, 2.5), 6)
        # tokens_minted is a value between 1_000 and 1_000_000
        tokens_minted = round(random.uniform(1_000, 1_000_000), 4)
        rows.append({
            "block": block,
            "vault_health": vault_health,
            "tokens_minted": tokens_minted,
        })

    # Sort by block for consistency
    rows.sort(key=lambda r: r["block"])

    # Write CSV
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["block", "vault_health", "tokens_minted"])
    writer.writeheader()
    writer.writerows(rows)

    csv_text = buf.getvalue()
    csv_bytes = csv_text.encode("utf-8")

    gt_path.write_bytes(csv_bytes)
    log.info(
        "Generated ground-truth CSV at %s (%d rows, %d bytes).",
        gt_path,
        len(rows),
        len(csv_bytes),
    )

    return csv_bytes


def _get_num_vaults_for_date(eval_date: str) -> int:
    """
    Determine how many vault rows to generate for a given date.

    Placeholder: returns a fixed small number.
    In production this would query on-chain vault count for that block range.
    """
    # Use a deterministic seed based on the date so re-runs are consistent.
    seed = hash(eval_date) & 0xFFFF_FFFF
    rng = random.Random(seed)
    return rng.randint(5, 20)


def ground_truth_path(eval_date: str) -> Path:
    """Return the expected path for an eval-date's ground-truth CSV."""
    return settings.ground_truth_dir / eval_date / "ground-truth.csv"


def result_path(eval_date: str) -> Path:
    """Return the expected path for an eval-date's result CSV."""
    return settings.ground_truth_dir / eval_date / "result.csv"


def append_result(
    eval_date: str,
    uid: int | str,
    hotkey: str,
    score: float,
) -> None:
    """
    Append one row to ``ground-truth/<eval_date>/result.csv``.

    The file is created if it does not exist.  Format::

        uid, hotkey, score
    """
    path = result_path(eval_date)
    path.parent.mkdir(parents=True, exist_ok=True)

    write_header = not path.exists()

    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["uid", "hotkey", "score"])
        writer.writerow([uid, hotkey, score])

    log.debug("Appended result to %s: uid=%s hotkey=%s score=%.4f", path, uid, hotkey, score)


def is_already_scored(eval_date: str, uid: int | str, hotkey: str) -> bool:
    """
    Check whether a (uid, hotkey) pair has already been scored for *eval_date*
    by reading ``ground-truth/<eval_date>/result.csv``.
    """
    path = result_path(eval_date)
    if not path.exists():
        return False

    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("uid") == str(uid) and row.get("hotkey") == hotkey:
                    return True
    except Exception as exc:
        log.warning("Error reading %s: %s — assuming not scored.", path, exc)

    return False