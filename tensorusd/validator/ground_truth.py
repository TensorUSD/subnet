"""
Ground-truth CSV generation for Phase 2 scoring.

Ground-truth data is generated from on-chain vault snapshots and
stored at::

    ground-truth/<eval_date>/ground-truth.csv

Each row contains:
    snapshot_hour, snapshot_time_utc, block_number, vault_owner, vault_id,
    vault_health, tokens_minted

The ground-truth CSV is derived from ``data.csv`` produced by the
``GroundTruthCollectator``, which collects 24 hourly on-chain vault
snapshots incrementally.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from tensorusd.auth.config import settings
from tensorusd.utils.logging import get_logger

log = get_logger(__name__)

# Columns expected in the ground-truth CSV
GT_FIELDNAMES = [
    "snapshot_hour",
    "snapshot_time_utc",
    "block_number",
    "vault_owner",
    "vault_id",
    "vault_health",
    "tokens_minted",
]


def generate_ground_truth(eval_date: str) -> bytes:
    """
    Generate a ground-truth CSV for the given *eval_date* (ISO-8601 date string).

    The ground truth is derived from the real on-chain vault snapshot data
    collected by the ``GroundTruthCollector`` into
    ``ground-truth/<eval_date>/data.csv``.

    Two derived columns are computed from the raw snapshot data:

        - ``vault_health``        = collateral_balance / borrowed_token_balance
                                  (0.0 when borrowed_token_balance is 0)
        - ``tokens_minted``       = borrowed_token_balance

    If ``data.csv`` does not exist for the given date, a warning is logged
    and an empty CSV (headers only) is returned.  If ``ground-truth.csv``
    already exists it is returned as-is.
    """
    gt_dir = settings.ground_truth_dir / eval_date
    gt_path = gt_dir / "ground-truth.csv"

    # Return existing file if present
    if gt_path.exists():
        log.info("Ground-truth CSV already exists at %s — reusing.", gt_path)
        return gt_path.read_bytes()

    data_csv_path = gt_dir / "data.csv"

    # Cannot generate ground truth without data
    if not data_csv_path.exists():
        log.warning(
            "data.csv not found at %s — cannot generate ground truth for %s. "
            "Returning empty CSV.",
            data_csv_path,
            eval_date,
        )
        empty_csv = b"snapshot_hour,snapshot_time_utc,block_number,vault_owner,vault_id,vault_health,tokens_minted\n"
        gt_dir.mkdir(parents=True, exist_ok=True)
        gt_path.write_bytes(empty_csv)
        return empty_csv

    # Read the raw on-chain snapshot data
    with open(data_csv_path, newline="") as f:
        reader = csv.DictReader(f)
        raw_rows = list(reader)

    if not raw_rows:
        log.warning(
            "data.csv at %s is empty — returning empty ground-truth CSV.",
            data_csv_path,
        )
        empty_csv = b"snapshot_hour,snapshot_time_utc,block_number,vault_owner,vault_id,vault_health,tokens_minted\n"
        gt_dir.mkdir(parents=True, exist_ok=True)
        gt_path.write_bytes(empty_csv)
        return empty_csv

    # Derive ground-truth columns from raw snapshot data
    rows: list[dict[str, str | float | int]] = []
    for raw in raw_rows:
        try:
            collateral = float(raw.get("collateral_balance", 0) or 0)
            borrowed = float(raw.get("borrowed_token_balance", 0) or 0)
        except (ValueError, TypeError):
            collateral = 0.0
            borrowed = 0.0

        vault_health = collateral / borrowed if borrowed > 0 else 0.0

        rows.append({
            "snapshot_hour": int(raw.get("snapshot_hour", 0) or 0),
            "snapshot_time_utc": raw.get("snapshot_time_utc", ""),
            "block_number": int(raw.get("block_number", 0) or 0),
            "vault_owner": raw.get("vault_owner", ""),
            "vault_id": int(raw.get("vault_id", 0) or 0),
            "vault_health": round(vault_health, 6),
            "tokens_minted": round(borrowed, 4),
        })

    # Sort by snapshot_hour, block_number, vault_owner, vault_id for consistency
    rows.sort(key=lambda r: (r["snapshot_hour"], r["block_number"], r["vault_owner"], r["vault_id"]))

    # Write CSV
    gt_dir.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=GT_FIELDNAMES)
    writer.writeheader()
    writer.writerows(rows)

    csv_text = buf.getvalue()
    csv_bytes = csv_text.encode("utf-8")

    gt_path.write_bytes(csv_bytes)
    log.info(
        "Generated ground-truth CSV at %s (%d rows, %d bytes) derived from %s",
        gt_path,
        len(rows),
        len(csv_bytes),
        data_csv_path.name,
    )

    return csv_bytes


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