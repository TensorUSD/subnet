"""
Ground-truth CSV generation for Phase 2 scoring.

Ground-truth data is generated from on-chain vault snapshots and
stored at::

    ground-truth/<current-date>/ground-truth.csv

Each row contains:
    snapshot_hour, vault_owner, vault_id, vault_health, tokens_minted

The ground-truth CSV is derived from ``data.csv`` produced by the
``GroundTruthCollector``, which collects 24 hourly on-chain vault
snapshots incrementally.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from pathlib import Path

from tensorusd.auth.config import settings
from tensorusd.utils.logging import get_logger

log = get_logger(__name__)

# Columns expected in the ground-truth CSV
GT_FIELDNAMES = [
    "snapshot_hour",
    "vault_owner",
    "vault_id",
    "vault_health",
    "tokens_minted",
]

EMPTY_CSV = (
    b"snapshot_hour,vault_owner,vault_id,"
    b"vault_health,tokens_minted\n"
)

DEFAULT_GROUND_TRUTH_DIR = settings.ground_truth_dir
DEFAULT_SCORING_DELAY = settings.scoring_delay_days



def _read_data_csv(path: Path) -> list[dict[str, str]]:
    """Read a CSV file and return its rows as dictionaries.

    Args:
        path: Path to the CSV file.

    Returns:
        A list of dictionaries, where each dictionary represents a row
        in the CSV file with column headers as keys.
    """
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)



def _borrowed_balance_lookup(raw_rows: list[dict[str, str]]) -> dict[tuple[str, int], float]:
    """
    Build a ``(vault_owner, vault_id) -> borrowed_token_balance`` lookup from
    raw snapshot rows.

    Args:
        raw_rows: List of raw snapshot dictionaries. Each dict is expected
            to contain ``vault_owner``, ``vault_id``, and
            ``borrowed_token_balance`` keys.

    Returns:
        A dictionary mapping ``(vault_owner, vault_id)`` tuples to their
        most recent borrowed token balance as a float.
    """
    lookup: dict[tuple[str, int], float] = {}
    for raw in raw_rows:
        try:
            vault_id = int(raw.get("vault_id", 0) or 0)
            borrowed = float(raw.get("borrowed_token_balance", 0) or 0)
        except (ValueError, TypeError):
            continue
        vault_owner = raw.get("vault_owner", "")
        lookup[(vault_owner, vault_id)] = borrowed
    return lookup

def _delayed_date_str(eval_date: str, scoring_delay_days: int) -> str | None:
    """Return the ISO date string for ``eval_date - scoring_delay_days``.
    
    Args:
        eval_date: A valid ISO-8601 date string (e.g., ``"2025-07-15"``).
        scoring_delay_days: Number of days to subtract from ``eval_date``.
            May be negative to compute a future date.

    Returns:
        The resulting date as an ISO-8601 string, or ``None`` if
        ``eval_date`` cannot be parsed as a valid ISO-8601 date.
    """
    try:
        target = date.fromisoformat(eval_date)
    except ValueError:
        log.warning("eval_date %s is not a valid ISO-8601 date — skipping delay lookup.", eval_date)
        return None
    return (target - timedelta(days=scoring_delay_days)).isoformat()


def generate_ground_truth(eval_date: str) -> bytes:
    """
    Generate a ground-truth CSV for the given *eval_date* (ISO-8601 date string).

    The ground truth is derived from the real on-chain vault snapshot data
    collected by the ``GroundTruthCollector`` into
    ``ground-truth/<eval_date>/data.csv``.
    
    Args:
        eval_date: ISO-8601 date string identifying the evaluation snapshot
            (e.g., ``"2025-07-15"``).

    Returns:
        The ground-truth CSV content as UTF-8 encoded bytes. Returns an
        empty CSV (headers only) when source data is unavailable.

    Note:
        Two derived columns are computed from the raw snapshot data:

        - ``vault_health``  = collateral_balance / borrowed_token_balance
                               (0.0 when borrowed_token_balance is 0)
        - ``tokens_minted``:
            - If a ``data.csv`` also exists for
              ``eval_date - DEFAULT_SCORING_DELAY``, then for each
              vault (matched by ``vault_owner`` + ``vault_id``):
                  tokens_minted = borrowed_now - borrowed_delayed
              (falling back to the normal computation for any vault not
              found in the delayed snapshot).
            - Otherwise: tokens_minted = borrowed_token_balance (current
              behavior).

        If ``data.csv`` does not exist for the given date, a warning is logged
        and an empty CSV (headers only) is returned.  If ``ground-truth.csv``
        already exists it is returned as-is (nothing is regenerated).
    """
    gt_dir = DEFAULT_GROUND_TRUTH_DIR / eval_date
    gt_path = gt_dir / "ground-truth.csv"

    # 1. Return existing file if present -- do nothing else.
    if gt_path.exists():
        log.info("Ground-truth CSV already exists at %s — reusing.", gt_path)
        return gt_path.read_bytes()

    data_csv_path = gt_dir / "data.csv"

    # 2. Cannot generate ground truth without data.
    if not data_csv_path.exists():
        log.warning(
            "data.csv not found at %s — cannot generate ground truth for %s. "
            "Returning empty CSV.",
            data_csv_path,
            eval_date,
        )
        gt_dir.mkdir(parents=True, exist_ok=True)
        gt_path.write_bytes(EMPTY_CSV)
        return EMPTY_CSV

    # Read the raw on-chain snapshot data for the current date.
    raw_rows = _read_data_csv(data_csv_path)

    if not raw_rows:
        log.warning(
            "data.csv at %s is empty — returning empty ground-truth CSV.",
            data_csv_path,
        )
        gt_dir.mkdir(parents=True, exist_ok=True)
        gt_path.write_bytes(EMPTY_CSV)
        return EMPTY_CSV

    # 3. Look for the delayed date's data.csv (target_date - scoring_delay_days).
    scoring_delay_days = DEFAULT_SCORING_DELAY
    delayed_lookup: dict[tuple[str, int], float] = {}
    delayed_date = _delayed_date_str(eval_date, scoring_delay_days)

    if delayed_date is not None:
        delayed_data_csv_path = DEFAULT_GROUND_TRUTH_DIR / delayed_date / "data.csv"
        if delayed_data_csv_path.exists():
            delayed_raw_rows = _read_data_csv(delayed_data_csv_path)
            delayed_lookup = _borrowed_balance_lookup(delayed_raw_rows)
            log.info(
                "Using delayed-difference tokens_minted computation: %s (delay=%d days) "
                "found with %d vault entries.",
                delayed_data_csv_path,
                scoring_delay_days,
                len(delayed_lookup),
            )
        else:
            log.info(
                "Delayed data.csv not found at %s — falling back to current-balance "
                "tokens_minted computation for %s.",
                delayed_data_csv_path,
                eval_date,
            )

    # Derive ground-truth columns from raw snapshot data.
    rows: list[dict[str, str | float | int]] = []
    for raw in raw_rows:
        try:
            collateral = float(raw.get("collateral_balance", 0) or 0)
            borrowed = float(raw.get("borrowed_token_balance", 0) or 0)
        except (ValueError, TypeError):
            collateral = 0.0
            borrowed = 0.0

        vault_health = collateral / borrowed if borrowed > 0 else 0.0

        vault_owner = raw.get("vault_owner", "")
        try:
            vault_id = int(raw.get("vault_id", 0) or 0)
        except (ValueError, TypeError):
            vault_id = 0

        # Prefer delayed-difference method when the vault is present in the
        # delayed snapshot; otherwise fall back to the existing logic.
        borrowed_delayed = delayed_lookup.get((vault_owner, vault_id))
        if borrowed_delayed is not None:
            tokens_minted = borrowed - borrowed_delayed
        else:
            tokens_minted = borrowed

        rows.append({
            "snapshot_hour": int(raw.get("snapshot_hour", 0) or 0),
            "vault_owner": vault_owner,
            "vault_id": vault_id,
            "vault_health": round(vault_health, 6),
            "tokens_minted": round(tokens_minted, 4),
        })

    # Sort by snapshot_hour, block_number, vault_owner, vault_id for consistency
    rows.sort(key=lambda r: (r["snapshot_hour"], r["vault_owner"], r["vault_id"]))

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
    return DEFAULT_GROUND_TRUTH_DIR / eval_date / "ground-truth.csv"


def result_path(eval_date: str) -> Path:
    """Return the expected path for an eval-date's result CSV."""
    return DEFAULT_GROUND_TRUTH_DIR / eval_date / "result.csv"



def append_result(
    eval_date: str,
    uid: int | str,
    hotkey: str,
    score: float,
) -> None:
    """
    Append one row to ``ground-truth/<eval_date>/result.csv``.

    Args:
        eval_date: ISO-8601 date string identifying the evaluation run.
        uid: Unique identifier for the scored entity (miner/neuron).
        hotkey: SS58-encoded hotkey associated with the scored entity.
        score: Computed score value; stored with full float precision in CSV.
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
    
    Args:
        eval_date: ISO-8601 date string identifying the evaluation run.
        uid: Unique identifier to check. Converted to string internally
            to match CSV storage format.
        hotkey: SS58-encoded hotkey to check. Compared as-is.

    Returns:
        ``True`` if a matching row is found, ``False`` otherwise. Also
        returns ``False`` when the result file does not exist or cannot
        be read (fail-open semantics).
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