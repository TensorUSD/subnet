"""
Ground-truth CSV generation for Phase 2 scoring.

Ground-truth data is generated from on-chain vault snapshots and
stored at::

    ground-truth/<eval_date>/ground-truth.csv

Each row contains:
    date, vault_owner, vault_id,
    vault_health, tokens_minted

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
    "date",
    "vault_owner",
    "vault_id",
    "vault_health",
    "tokens_minted",
]

EMPTY_CSV = (
    b"date,vault_owner,vault_id,"
    b"vault_health,tokens_minted\n"
)

DEFAULT_GROUND_TRUTH_DIR = settings.ground_truth_dir
DEFAULT_SCORING_DELAY = settings.scoring_delay_days
LAST_HOUR_OF_DAY = 23
DEFAULT_WINDOW_DAYS = 7


def _read_data_csv(path: Path) -> list[dict[str, str]]:
    """Read a raw snapshot ``data.csv`` file into a list of dict rows."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _parse_snapshot_hour(raw: dict[str, str]) -> int | None:
    """Extract the integer ``snapshot_hour`` from a raw row (None if invalid)."""
    try:
        return int(raw.get("snapshot_hour", "") or -1)
    except (ValueError, TypeError):
        return None


def _vault_key(raw: dict[str, str]) -> tuple[str, int]:
    """Return the ``(vault_owner, vault_id)`` identity used across lookups."""
    try:
        vault_id = int(raw.get("vault_id", 0) or 0)
    except (ValueError, TypeError):
        vault_id = 0
    return (raw.get("vault_owner", ""), vault_id)


def _data_csv_path_for(date_str: str) -> Path:
    """Return the raw snapshot ``data.csv`` path for an ISO date string."""
    return DEFAULT_GROUND_TRUTH_DIR / date_str / "data.csv"


def _select_last_hour_rows(
    raw_rows: list[dict[str, str]],
    date_str: str,
) -> list[dict[str, str]]:
    """
    Select the rows forming *date_str*'s ground truth: only the snapshots
    taken during the **last hour of the day** (hour 23).

    If hour 23 was never collected (e.g. the validator was down at the day
    boundary), the latest hour actually collected that day is used instead.
    """
    by_hour: dict[int, list[dict[str, str]]] = {}
    for raw in raw_rows:
        hour = _parse_snapshot_hour(raw)
        if hour is None or hour < 0:
            continue
        by_hour.setdefault(hour, []).append(raw)

    if not by_hour:
        return []

    if LAST_HOUR_OF_DAY in by_hour:
        selected_hour = LAST_HOUR_OF_DAY
    else:
        selected_hour = max(by_hour)
        log.warning(
            "Last hour %d missing for %s — falling back to the latest "
            "collected hour %d (%d row(s)).",
            LAST_HOUR_OF_DAY,
            date_str,
            selected_hour,
            len(by_hour[selected_hour]),
        )

    deduped: dict[tuple[str, int], dict[str, str]] = {}
    for raw in by_hour[selected_hour]:
        deduped[_vault_key(raw)] = raw

    return list(deduped.values())


def _window_dates(eval_date: str, window_days: int) -> list[str]:
    """
    Return the ISO date strings of the rolling window ending at *eval_date*
    (inclusive), ordered oldest → newest.
    """
    end = date.fromisoformat(eval_date)
    return [
        (end - timedelta(days=offset)).isoformat()
        for offset in range(window_days - 1, -1, -1)
    ]


def _borrowed_balance_lookup(raw_rows: list[dict[str, str]]) -> dict[tuple[str, int], float]:
    """
    Build a ``(vault_owner, vault_id) -> borrowed_token_balance`` lookup from
    raw snapshot rows.

    If a vault appears multiple times (e.g. duplicate appends), the last
    row wins, since raw rows are assumed to be in chronological order.
    """
    lookup: dict[tuple[str, int], float] = {}
    for raw in raw_rows:
        try:
            borrowed = float(raw.get("borrowed_token_balance", 0) or 0)
        except (ValueError, TypeError):
            continue
        lookup[_vault_key(raw)] = borrowed
    return lookup


def _delayed_date_str(eval_date: str, scoring_delay_days: int) -> str | None:
    """Return the ISO date string for ``eval_date - scoring_delay_days``."""
    try:
        target = date.fromisoformat(eval_date)
    except ValueError:
        log.warning("eval_date %s is not a valid ISO-8601 date — skipping delay lookup.", eval_date)
        return None
    return (target - timedelta(days=scoring_delay_days)).isoformat()


def generate_ground_truth(
    eval_date: str,
    window_days: int = DEFAULT_WINDOW_DAYS,
    scoring_delay_days: int | None = None,
) -> bytes:
    """
    Generate a ground-truth CSV for the given *eval_date* (ISO-8601 date string).


    Two derived columns are computed from the raw snapshot data:

        - ``vault_health``  = collateral_balance / borrowed_token_balance
                               (0.0 when borrowed_token_balance is 0)
        - ``tokens_minted``, per row's own day ``d``
            - If a ``data.csv`` also exists for ``d - scoring_delay_days``
              with last-hour data, then for each vault (matched by
              ``vault_owner`` + ``vault_id``):
                  tokens_minted = borrowed(d) - borrowed(d - delay)
              (falling back to the normal computation for any vault not
              found in the delayed snapshot).
            - Otherwise: tokens_minted = borrowed_token_balance.

    If no ``data.csv`` exists for any day in the window, a warning is
    logged and an empty CSV (headers only) is returned.  If
    ``ground-truth.csv`` already exists it is returned as-is (nothing is
    regenerated).
    """
    gt_dir = DEFAULT_GROUND_TRUTH_DIR / eval_date
    gt_path = gt_dir / "ground-truth.csv"

    # 1. Return existing file if present -- do nothing else.
    if gt_path.exists():
        log.info("Ground-truth CSV already exists at %s — reusing.", gt_path)
        return gt_path.read_bytes()

    # Resolve the rolling window (eval_date + the previous N-1 days).
    try:
        dates = _window_dates(eval_date, window_days)
    except ValueError:
        log.warning(
            "eval_date %s is not a valid ISO-8601 date — using a single-day window.",
            eval_date,
        )
        dates = [eval_date]

    if scoring_delay_days is None:
        scoring_delay_days = DEFAULT_SCORING_DELAY

    # Collect the last-hour rows for each day in the window. Days with
    # no data simply contribute nothing.
    window_rows: list[tuple[str, list[dict[str, str]]]] = []
    for day_str in dates:
        data_csv_path = _data_csv_path_for(day_str)
        if not data_csv_path.exists():
            log.info("No data.csv for %s — day skipped in ground-truth window.", day_str)
            continue
        raw_rows = _read_data_csv(data_csv_path)
        if not raw_rows:
            log.info("data.csv for %s is empty — day skipped in ground-truth window.", day_str)
            continue

        selected = _select_last_hour_rows(raw_rows, day_str)
        if not selected:
            log.warning(
                "No parsable snapshot hours in %s — day skipped in ground-truth window.",
                data_csv_path,
            )
            continue
        window_rows.append((day_str, selected))

    # Cannot generate ground truth without any data at all.
    if not window_rows:
        log.warning(
            "No snapshot data found for any day in [%s .. %s] — "
            "returning empty ground-truth CSV.",
            dates[0],
            dates[-1],
        )
        gt_dir.mkdir(parents=True, exist_ok=True)
        gt_path.write_bytes(EMPTY_CSV)
        return EMPTY_CSV

    # Cached delayed-difference lookups: day -> {(owner, id): borrowed}.
    delayed_lookups: dict[str, dict[tuple[str, int], float]] = {}

    def _delayed_lookup_for(day_str: str) -> dict[tuple[str, int], float]:
        if day_str in delayed_lookups:
            return delayed_lookups[day_str]

        lookup: dict[tuple[str, int], float] = {}
        delayed_date = _delayed_date_str(day_str, scoring_delay_days)
        if delayed_date is not None:
            delayed_data_csv_path = _data_csv_path_for(delayed_date)
            if delayed_data_csv_path.exists():
                delayed_raw_rows = _read_data_csv(delayed_data_csv_path)
                delayed_selected = _select_last_hour_rows(delayed_raw_rows, delayed_date)
                lookup = _borrowed_balance_lookup(delayed_selected)

        delayed_lookups[day_str] = lookup
        return lookup

    # Derive ground-truth columns, oldest day first so equal sort keys
    # below keep chronological order (the final sort is stable).
    rows: list[dict[str, str | float | int]] = []
    for day_str, selected in window_rows:
        delayed_lookup = _delayed_lookup_for(day_str)

        for raw in selected:
            try:
                collateral = float(raw.get("collateral_balance", 0) or 0)
                borrowed = float(raw.get("borrowed_token_balance", 0) or 0)
            except (ValueError, TypeError):
                collateral = 0.0
                borrowed = 0.0

            vault_health = collateral / borrowed if borrowed > 0 else 0.0
            vault_owner, vault_id = _vault_key(raw)

            # Prefer delayed-difference method when the vault is present in
            # the delayed snapshot; otherwise fall back to the existing logic.
            borrowed_delayed = delayed_lookup.get((vault_owner, vault_id))
            if borrowed_delayed is not None:
                tokens_minted = borrowed - borrowed_delayed
            else:
                tokens_minted = borrowed

            rows.append({
                "date": day_str,
                "vault_owner": vault_owner,
                "vault_id": vault_id,
                "vault_health": round(vault_health, 6),
                "tokens_minted": round(tokens_minted, 4),
            })

    # Sort by date (ISO dates sort chronologically), vault_owner, vault_id
    # for consistency.
    rows.sort(key=lambda r: (r["date"], r["vault_owner"], r["vault_id"]))

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
        "Generated ground-truth CSV at %s (%d rows, %d bytes) from %d day(s) "
        "in window [%s .. %s], last hour %d only",
        gt_path,
        len(rows),
        len(csv_bytes),
        len(window_rows),
        window_rows[0][0],
        window_rows[-1][0],
        LAST_HOUR_OF_DAY,
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