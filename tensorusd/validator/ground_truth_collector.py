"""
Ground-truth collector — runs in a background thread inside the validator.

Collects 24 hourly vault snapshots incrementally — one snapshot per hour
as each hour passes, then builds ``ground-truth.csv`` at the end of the day.

Usage (internal — called by ValidatorCore):
    collector = GroundTruthCollector(wallet)
    collector.start()   # launches background thread
    collector.stop()    # stops the thread

The collector checks once per minute:
  1. Find the **last completed UTC hour** (e.g., if it's 14:05, the
     last completed hour is 14:00 — hour 14).
  2. Check if ``data.csv`` already has a row for that hour.
  3. If not → query chain at that hour's block → append to ``data.csv``.
  4. Once ``data.csv`` has 24 rows → build ``ground-truth.csv``.
"""

from __future__ import annotations

import csv
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import bittensor as bt

from tensorusd.common.contract import (
    TensorUSDVaultContract,
    create_substrate_interface,
    Vault,
)
from tensorusd.auth.config import settings
from tensorusd.utils.logging import get_logger

log = get_logger(__name__)

# Constants

PAGE_SIZE = 10

DEFAULT_RPC_ENDPOINTS = {
    "finney": [
        "wss://entrypoint-finney.opentensor.ai:443",
        "wss://finney.opentensor.ai:443",
        "wss://entrypoint-finance.opentensor.ai:443",
    ],
    "testnet": ["wss://test.finney.opentensor.ai:443"],
}

# Ground-truth directory (same default as tensorusd/auth/config.py)
GROUND_TRUTH_DIR = Path(os.environ.get("TENSORUSD_GROUND_TRUTH_DIR", "ground-truth"))

# How often to check if it's time to collect (seconds)
POLL_INTERVAL = 60


# Vault helpers (silent — no verbose logging)


def _get_total_vaults_count(
    contract: TensorUSDVaultContract, block_hash: Optional[bytes] = None
) -> int:
    try:
        result = contract.contract.read(
            keypair=contract.wallet.hotkey,
            method="get_total_vaults_count",
            block_hash=block_hash,
        )
        data = result.contract_result_data.value_object
        if data and data[0] == "Ok":
            return data[1].value
        return 0
    except Exception as e:
        log.warning("get_total_vaults_count failed: %s", e)
        return 0


def _get_all_vaults_page(
    contract: TensorUSDVaultContract,
    page: int,
    block_hash: Optional[bytes] = None,
) -> List[Tuple[str, int]]:
    try:
        result = contract.contract.read(
            keypair=contract.wallet.hotkey,
            method="get_all_vaults",
            args={"page": page},
            block_hash=block_hash,
        )
        data = result.contract_result_data.value_object
    except Exception as e:
        log.warning("get_all_vaults(page=%d) failed: %s", page, e)
        return []

    vaults: List[Tuple[str, int]] = []
    if data and data[0] == "Ok" and data[1]:
        raw_list = data[1].value
        if isinstance(raw_list, list):
            for entry in raw_list:
                if isinstance(entry, dict):
                    if "Ok" in entry and isinstance(entry["Ok"], (list, tuple)) and len(entry["Ok"]) >= 2:
                        vaults.append((entry["Ok"][0], int(entry["Ok"][1])))
                    elif "owner" in entry and "vault_id" in entry:
                        vaults.append((entry["owner"], int(entry["vault_id"])))
                    elif "id" in entry and "owner" in entry:
                        vaults.append((entry["owner"], int(entry["id"])))
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    vaults.append((entry[0], int(entry[1])))
        elif isinstance(raw_list, dict) and "Ok" in raw_list:
            inner = raw_list["Ok"]
            if isinstance(inner, list):
                for entry in inner:
                    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        vaults.append((entry[0], int(entry[1])))
                    elif isinstance(entry, dict):
                        if "owner" in entry and "vault_id" in entry:
                            vaults.append((entry["owner"], int(entry["vault_id"])))
                        elif "id" in entry and "owner" in entry:
                            vaults.append((entry["owner"], int(entry["id"])))
    return vaults


def discover_all_vaults(
    contract: TensorUSDVaultContract,
    block_hash: Optional[bytes] = None,
) -> List[Tuple[str, int]]:
    total = _get_total_vaults_count(contract, block_hash=block_hash)
    if total == 0:
        return []
    total_pages = math.ceil(total / PAGE_SIZE)
    all_vaults: List[Tuple[str, int]] = []
    for page in range(total_pages):
        all_vaults.extend(_get_all_vaults_page(contract, page, block_hash=block_hash))

    seen = set()
    unique = []
    for owner, vid in all_vaults:
        key = (owner, vid)
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def _get_vault_at_block(
    contract: TensorUSDVaultContract,
    owner: str,
    vault_id: int,
    block_hash: Optional[bytes] = None,
) -> Optional[Vault]:
    try:
        result = contract.contract.read(
            keypair=contract.wallet.hotkey,
            method="get_vault",
            args={"owner": owner, "vault_id": vault_id},
            block_hash=block_hash,
        )
        data = result.contract_result_data.value_object
        if data and data[0] == "Ok" and data[1]:
            vault_data = data[1].value_object
            return Vault(
                id=vault_data["id"],
                owner=vault_data["owner"],
                collateral_balance=vault_data["collateral_balance"],
                borrowed_token_balance=vault_data["borrowed_token_balance"],
                created_at=vault_data["created_at"],
                last_interest_accrued_at=vault_data["last_interest_accrued_at"],
            )
        return None
    except Exception as e:
        log.warning("get_vault(%s, %d) failed: %s", owner, vault_id, e)
        return None


# Block / timestamp helpers


def _get_chain_timestamp_at_block(substrate, block_number: int) -> int:
    """Query Timestamp::Now at a specific block number."""
    try:
        block_hash = substrate.get_block_hash(block_number)
        if block_hash is None:
            return 0
        result = substrate.query("Timestamp", "Now", block_hash=block_hash)
        return result.value if result else 0
    except Exception as e:
        log.warning("Error querying timestamp at block %d: %s", block_number, e)
        return 0


def _estimate_block_at_timestamp(
    substrate, target_timestamp_ms: int, max_block: Optional[int] = None
) -> int:
    current_block = substrate.get_block_number(None)
    current_timestamp = substrate.query("Timestamp", "Now").value

    if not current_timestamp or current_timestamp <= 0:
        raise RuntimeError("Failed to query current chain timestamp.")

    if max_block is not None and current_block > max_block:
        current_block = max_block
        bh = substrate.get_block_hash(max_block)
        current_timestamp = substrate.query("Timestamp", "Now", block_hash=bh).value

    ms_per_block = current_timestamp / current_block
    estimated = int(target_timestamp_ms / ms_per_block)
    return max(estimated, 1)


def _refine_block_estimate(
    substrate, target_timestamp_ms: int, initial_estimate: int
) -> int:
    best_diff = float("inf")
    best_block = initial_estimate

    for offset in range(-1000, 1001, 50):
        blk = max(initial_estimate + offset, 1)
        try:
            ts = _get_chain_timestamp_at_block(substrate, blk)
        except Exception:
            continue

# GroundTruthCollector class


class GroundTruthCollector:
    """
    Background thread that collects 24 hourly vault snapshots
    **incrementally** — one snapshot per hour as each hour passes.

    Instead of batching all 24 at once after 23:00, the collector
    polls every 60s and collects the **latest completed hour** that
    doesn't yet have a row in ``data.csv``.

    Once all 24 hours have been collected (``data.csv`` has 24 rows),
    it builds ``ground-truth.csv`` for the scoring pipeline.
    """

    def __init__(self, wallet: bt.Wallet) -> None:
        self._wallet = wallet
        self._stop = threading.Event()

        # Separate SubstrateInterface — not the validator's bt.Subtensor
        self._substrate = None
        self._contract = None

    def start(self) -> None:
        """Launch the background collector thread."""
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="gt-collector",
        )
        self._thread.start()
        log.info("Ground-truth collector thread started (daemon).")

    def stop(self) -> None:
        """Signal the collector to stop."""
        self._stop.set()
        log.info("Ground-truth collector stop signaled.")

    def _poll_loop(self) -> None:
        """
        Background poll loop — runs once per minute.

        Each iteration:
          1. Determine the **last completed UTC hour**.
             (e.g., if now is 14:05 UTC → last completed hour = 14,
              meaning the snapshot for "hour 14" should be available.)
          2. Check if ``data.csv`` for today already has a row for that hour.
          3. If not → query the chain at that hour's block → append row.
          4. When ``data.csv`` has 24 rows → build ``ground-truth.csv``.
          5. If ``ground-truth.csv`` already exists → skip (run once per day).
        """
        log.info("Ground-truth poll loop started (incremental mode).")

        while not self._stop.is_set():
            try:
                now = datetime.now(timezone.utc)
                today = now.strftime("%Y-%m-%d")
                data_dir = GROUND_TRUTH_DIR / today
                data_csv_path = data_dir / "data.csv"
                ground_truth_csv = data_dir / "ground-truth.csv"

                # --- Step 1: Determine last completed hour ---
                # If now is 14:05, the last fully-completed hour is 13
                # (hour 13 ran from 13:00–13:59).
                last_completed_hour = max(now.hour - 1, 0)  # hour 0..23

                # --- Step 2: Check if today is already done ---
                if ground_truth_csv.exists():
                    log.info("Ground-truth already exists for %s — skipping.", today)
                    time.sleep(POLL_INTERVAL)
                    continue

                # --- Step 3: Read existing data.csv (or create empty) ---
                existing_hours: set[int] = set()
                if data_csv_path.exists():
                    with open(data_csv_path, newline="") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            try:
                                existing_hours.add(int(row.get("hour", -1)))
                            except (ValueError, TypeError):
                                pass

                # --- Step 4: Collect any missing hours up to last_completed_hour ---
                data_dir.mkdir(parents=True, exist_ok=True)

                # Connect to chain via own SubstrateInterface
                substrate = create_substrate_interface(
                    rpc_url=urlparse(DEFAULT_RPC_ENDPOINTS["finney"][0]),
                )
                if not substrate:
                    log.warning("Failed to create substrate interface — retrying.")
                    time.sleep(POLL_INTERVAL)
                    continue

                # Collect **all** missing hours up to and including
                # the last completed one (in case we missed many).
                new_rows: list[dict] = []

                for hour in range(0, last_completed_hour + 1):
                    if hour in existing_hours:
                        continue  # already collected

                    log.info("Collecting hour %d for %s.", hour, today)
                    # Target timestamp for this hour's :00 (in ms)
                    target_ts_ms = int(
                        datetime(
                            now.year, now.month, now.day,
                            hour, 0, 0, tzinfo=timezone.utc,
                        ).timestamp() * 1000
                    )

                    # Find the block closest to that timestamp
                    estimate = _estimate_block_at_timestamp(substrate, target_ts_ms)
                    refined = _refine_block_estimate(substrate, target_ts_ms, estimate)

                    # Get vault snapshots at that block
                    block_hash = substrate.get_block_hash(refined)
                    if block_hash is None:
                        log.warning("No block hash for %d — skipping hour %d.", refined, hour)
                        continue

                    vaults = discover_all_vaults(
                        self._contract,
                        block_hash=block_hash,
                    )

                    for owner, vid in vaults:
                        v = _get_vault_at_block(
                            self._contract,
                            owner=owner,
                            vault_id=vid,
                            block_hash=block_hash,
                        )
                        if v:
                            new_rows.append({
                                "block": refined,
                                "hour": hour,
                                "owner": v["owner"],
                                "vault_id": v["id"],
                                "collateral": v["collateral_balance"],
                                "borrowed": v["borrowed_token_balance"],
                            })

                    existing_hours.add(hour)

                # --- Step 5: Append new rows to data.csv ---
                if new_rows:
                    # Write mode: 'a' if file exists, 'w' if not
                    mode = "a" if data_csv_path.exists() else "w"
                    with open(data_csv_path, mode, newline="") as f:
                        writer = csv.DictWriter(
                            f,
                            fieldnames=["block", "hour", "owner", "vault_id", "collateral", "borrowed"],
                        )
                        if mode == "w":
                            writer.writeheader()
                        writer.writerows(new_rows)

                    log.info(
                        "Appended %d new rows for %s (hours collected so far: %d).",
                        len(new_rows),
                        today,
                        len(existing_hours),
                    )

                # --- Step 6: Check if all 24 hours are done ---
                if len(existing_hours) >= 24:
                    log.info("All 24 hours collected for %s — building ground-truth.csv.", today)
                    # Build ground-truth.csv (calls ground_truth.py logic)
                    from tensorusd.validator.ground_truth import generate_ground_truth
                    generate_ground_truth(today)

                # --- Step 7: Sleep ---
                time.sleep(POLL_INTERVAL)

            except Exception as exc:
                log.error("Ground-truth poll error: %s", exc, exc_info=True)
                time.sleep(POLL_INTERVAL)