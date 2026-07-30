"""
Ground-truth collector — runs in a background thread inside the validator.

Collects 24 hourly vault snapshots incrementally — one snapshot per hour
as each hour passes, then builds ``ground-truth.csv`` at the end of the day.

The collector polls once per minute and uses **chain timestamps** to find the
block closest to each hour's ``:00`` mark (e.g. 14:00 UTC), rather than
snapshotting the current block.

Usage (internal — called by ValidatorCore):
    collector = GroundTruthCollector(wallet)
    collector.start()   # launches background thread
    collector.stop()    # stops the thread
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple
from urllib.parse import urlparse

import bittensor as bt

from tensorusd.common.contract import (
    TensorUSDVaultContract,
    create_substrate_interface,
)
from tensorusd.utils.config import add_validator_args
from tensorusd.utils.logging import get_logger
from tensorusd.validator.ground_truth import generate_ground_truth

log = get_logger(__name__)

# Constants
PAGE_SIZE = 10

DEFAULT_RPC_ENDPOINTS = {
    "finney": [
        "wss://entrypoint-finney.opentensor.ai:443",
    ],
    "testnet": ["wss://test.finney.opentensor.ai:443"],
}

# Ground-truth directory (same default as tensorusd/auth/config.py)
GROUND_TRUTH_DIR = Path("ground-truth")

# How often to check if it's time to collect (seconds)
POLL_INTERVAL = 600
CAPTURE_WINDOW_MIN = 30

FIELDNAMES = [
    "snapshot_hour",
    "snapshot_time_utc",
    "block_number",
    "vault_owner",
    "vault_id",
    "collateral_balance",
    "borrowed_token_balance",
    "created_at",
    "last_interest_accrued_at",
]


def _is_ws_url(value: str) -> bool:
    return urlparse(value).scheme in {"ws", "wss"}


def _create_substrate_with_fallback(network: str, preferred_endpoint: str):
    """Create a substrate interface with fallback endpoints."""
    try:
        return create_substrate_interface(preferred_endpoint)
    except Exception as exc:
        last_error = exc
        log.warning("Failed to connect to %s: %s", preferred_endpoint, exc)

    raise RuntimeError(
        f"Unable to connect to any substrate endpoint for network '{network}'."
    ) from last_error


def _unwrap_ok(value: Any) -> Any:
    """
    Peel off nested Result::Ok wrappers / ScaleObj layers to a plain value.

    The substrateinterface contract SDK decodes a `Result<T, E>` as a
    2-element tuple ("Ok", <ScaleObj>) / ("Err", <ScaleObj>), and each
    ScaleObj needs `.value` (or `.value_object` for structs) to reach the
    real Python value. This handles arbitrarily nested Result wrappers
    (e.g. an outer call-level Result wrapping an inner method-level Result).
    """
    seen = 0
    while seen < 6:
        if isinstance(value, Tuple) and len(value) == 2 and value[0] in ("Ok", "Err"):
            if value[0] == "Err":
                raise RuntimeError(f"Contract call returned Err: {value[1]!r}")
            value = value[1]
        elif isinstance(value, Dict) and "Ok" in value and len(value) == 1:
            value = value["Ok"]
        elif hasattr(value, "value_object") and value.value_object is not None:
            value = value.value_object
        elif hasattr(value, "value"):
            value = value.value
        else:
            break
        seen += 1
    return value


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
        value = _unwrap_ok(data)
        return int(value) if value is not None else 0
    except Exception as e:
        log.warning("get_total_vaults_count failed: %s", e)
        return 0


def _get_all_vaults_page(
    contract: TensorUSDVaultContract,
    page: int,
    block_hash: Optional[bytes] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Call get_all_vaults(page) and return (vault_dicts, success).
    """
    try:
        result = contract.contract.read(
            keypair=contract.wallet.hotkey,
            method="get_all_vaults",
            args={"page": page},
            block_hash=block_hash,
        )
        page_vaults = _unwrap_ok(result["result"][1]["data"].value)
    except Exception as e:
        log.warning("get_all_vaults(page=%d) failed: %s", page, e)
        return [], False

    if not isinstance(page_vaults, List):
        log.warning(
            "get_all_vaults(page=%d) returned unexpected shape: %r",
            page,
            type(page_vaults),
        )
        return [], False

    return page_vaults, True


def discover_all_vaults(
    contract: TensorUSDVaultContract,
    block_hash: Optional[bytes] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch every vault by paginating through get_all_vaults, using
    get_total_vaults_count() as an upper bound on page count.

    Stops early on decode failure rather than spamming errors for every
    remaining page.
    """
    total = _get_total_vaults_count(contract, block_hash=block_hash)
    if total == 0:
        return []

    total_pages = math.ceil(total / PAGE_SIZE)
    all_vaults: List[Dict[str, Any]] = []

    for page in range(total_pages):
        page_vaults, ok = _get_all_vaults_page(contract, page, block_hash=block_hash)
        if not ok:
            log.info(
                "Stopping pagination at page %d (decode failed).",
                page,
            )
            break
        all_vaults.extend(page_vaults)
        if len(page_vaults) < PAGE_SIZE:
            # Natural last page — no point querying further.
            break

    # De-dupe on (owner, id) just in case of overlapping pages.
    seen = set()
    unique: List[Dict[str, Any]] = []
    for v in all_vaults:
        key = (v.get("owner"), v.get("id"))
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


def _get_current_hour_info(substrate) -> Tuple[str, int, float]:
    """
    Determine the CURRENT hour bucket using the chain timestamp — no
    rounding down to the *previous* completed hour, and no estimation.

    Returns:
        (date_str, hour, minutes_into_hour)
    """
    current_ts = substrate.query("Timestamp", "Now").value  # ms
    if not current_ts:
        raise RuntimeError("Failed to query current chain timestamp.")

    current_seconds = current_ts // 1000
    dt = datetime.fromtimestamp(current_seconds, tz=timezone.utc)
    date_str = dt.strftime("%Y-%m-%d")
    hour = dt.hour
    minutes_into_hour = (current_seconds % 3600) / 60.0
    return date_str, hour, minutes_into_hour


class GroundTruthCollector():
    """
    Background thread that collects hourly vault snapshots **at the current
    hour**.

    Each poll (every POLL_INTERVAL seconds):
      - Determines the current hour from the chain timestamp.
      - If that hour hasn't been collected yet AND we're still within
        CAPTURE_WINDOW_MIN minutes of the top of the hour, snapshot all
        vaults at the CURRENT block and append a row per vault to
        data.csv.
      - If we're past the capture window and the hour still isn't
        collected, that hour is permanently missed for the day (no
        backfill).
      - When the chain date rolls over, ground-truth.csv is built for the
        previous day from whatever hours were actually collected.
    """
    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        super().add_args(parser)
        add_validator_args(cls, parser, 1)

    def __init__(
        self,
        wallet: bt.Wallet,
        network: str,
        rpc_endpoint: str,
        vault_address: str,
        vault_metadata_path: str,
    ) -> None:
        
        self._wallet = wallet
        self._network = "finney"
        self._contract_address = vault_address
        self.rpc_endpoint = rpc_endpoint
        self.vault_metadata_path = vault_metadata_path
        self._stop = threading.Event()
        

        # Separate SubstrateInterface — not the validator's bt.Subtensor
        self._substrate = None
        self._contract = None
        self._metadata_path = "tensorusd/common/abis/tusdt_vault.json"

        # Tracks the last chain-date we saw, to detect day rollover.
        self._last_seen_date: Optional[str] = None

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

    def _ensure_connection(self) -> bool:
        """
        Ensure we have a working substrate connection and contract instance.
        Returns True if ready, False if retry needed.
        """
        try:
            substrate = _create_substrate_with_fallback(
                "finney",
                self.rpc_endpoint
            )
        except RuntimeError as e:
            log.warning("Failed to create substrate interface: %s", e)
            return False

        # Close previous connection if we had one
        if self._substrate is not None:
            try:
                self._substrate.close()
            except Exception:
                pass

        self._substrate = substrate
        self._contract = TensorUSDVaultContract(
            substrate=substrate,
            contract_address=self._contract_address,
            metadata_path=self._metadata_path,
            wallet=self._wallet,
        )

        return True

    def _get_current_block(self) -> Optional[int]:
        """
        Fetch the current block number via bt.SubtensorApi (a direct RPC
        call), falling back to substrate.get_block_number(None) only if
        SubtensorApi is unavailable.
        """
        if self._substrate is not None:
            try:
                return self._substrate.get_block_number(None)
            except Exception as e:
                log.warning(
                    "SubtensorApi.block failed (%s) — falling back to "
                    "substrate.get_block_number(None).",
                    e,
                )

    @staticmethod
    def _read_collected_hours(data_csv_path: Path) -> set[int]:
        collected: set[int] = set()
        if data_csv_path.exists():
            with open(data_csv_path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        collected.add(int(row.get("snapshot_hour", -1)))
                    except (ValueError, TypeError):
                        pass
        return collected

    def _finalize_day(self, date_str: str) -> None:
        """Build ground-truth.csv for `date_str` from whatever was collected."""
        data_dir = GROUND_TRUTH_DIR / date_str
        data_csv_path = data_dir / "data.csv"
        ground_truth_csv = data_dir / "ground-truth.csv"

        if ground_truth_csv.exists():
            return
        if not data_csv_path.exists():
            log.info("No data collected for %s — nothing to finalize.", date_str)
            return

        collected_hours = self._read_collected_hours(data_csv_path)
        log.info(
            "Finalizing %s — %d/24 hours collected.",
            date_str,
            len(collected_hours),
        )

        generate_ground_truth(date_str)
        
    def _finalize_previous_day_if_needed(self, current_date_str: str) -> None:
        """
        Check whether yesterday (relative to `current_date_str`) has a
        data.csv that was never finalized into ground-truth.csv, and finalize
        it if so. Handles the case where the collector was restarted (or
        started fresh) after a day boundary already passed, so
        `_finalize_day` never got triggered by the normal rollover check.
        """
        try:
            current_date = datetime.strptime(current_date_str, "%Y-%m-%d").date()
        except ValueError:
            log.warning("Could not parse current date %s — skipping backfill check.", current_date_str)
            return

        prev_date_str = (current_date - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_dir = GROUND_TRUTH_DIR / prev_date_str
        prev_data_csv = prev_dir / "data.csv"
        prev_ground_truth_csv = prev_dir / "ground-truth.csv"

        if prev_data_csv.exists() and not prev_ground_truth_csv.exists():
            log.info(
                "Found unfinalized data.csv for %s on startup — finalizing before "
                "resuming today's collection.",
                prev_date_str,
            )
            self._finalize_day(prev_date_str)

    def _poll_loop(self) -> None:
        """
        Background poll loop — runs once per minute.
        """
        try:
            if not self._ensure_connection():
                time.sleep(POLL_INTERVAL)
            else:
                date_str, _, _ = _get_current_hour_info(self._substrate)
                self._finalize_previous_day_if_needed(date_str)
                self._last_seen_date = date_str
        except Exception as exc:
            log.error("Startup backfill check failed: %s", exc, exc_info=True)
        # log.info("Ground-truth poll loop started (current-hour capture mode).")
        while not self._stop.is_set():
            try:
                if not self._ensure_connection():
                    time.sleep(POLL_INTERVAL)
                    continue
                substrate = self._substrate

                # --- Step 1: Determine the CURRENT hour via chain timestamp ---
                date_str, current_hour, minutes_into_hour = _get_current_hour_info(
                    substrate
                )

                # --- Step 2: Detect day rollover -> finalize previous day ---
                if (
                    self._last_seen_date is not None
                    and date_str != self._last_seen_date
                ):
                    self._finalize_day(self._last_seen_date)
                self._last_seen_date = date_str

                data_dir = GROUND_TRUTH_DIR / date_str
                data_csv_path = data_dir / "data.csv"
                ground_truth_csv = data_dir / "ground-truth.csv"

                if ground_truth_csv.exists():
                    # Already finalized for today somehow — nothing to do.
                    time.sleep(POLL_INTERVAL)
                    continue

                # --- Step 3: Skip if this hour is already collected ---
                existing_hours = self._read_collected_hours(data_csv_path)
                if current_hour in existing_hours:
                    time.sleep(POLL_INTERVAL)
                    continue

                # --- Step 4: Only capture within the capture window ---
                if minutes_into_hour > CAPTURE_WINDOW_MIN:
                    log.warning(
                        "Missed hour %d for %s — %.1f min into the hour, "
                        "Waiting for next hour.",
                        current_hour,
                        date_str,
                        minutes_into_hour,
                    )
                    time.sleep(POLL_INTERVAL)
                    continue

                # --- Step 5: Snapshot at the CURRENT block, no estimation ---
                data_dir.mkdir(parents=True, exist_ok=True)
                log.info(
                    "Collecting hour %d for %s (%.1f min into hour).",
                    current_hour,
                    date_str,
                    minutes_into_hour,
                )

                final_block = self._get_current_block()
                if final_block is None:
                    log.warning("Could not determine current block — retrying later.")
                    time.sleep(POLL_INTERVAL)
                    continue

                try:
                    block_hash = substrate.get_block_hash(final_block)
                except Exception as e:
                    log.warning(
                        "Failed to get block hash for block %d: %s",
                        final_block,
                        e,
                    )
                    time.sleep(POLL_INTERVAL)
                    continue

                if block_hash is None:
                    log.warning("Current block hash is None — retrying later.")
                    time.sleep(POLL_INTERVAL)
                    continue

                # --- Step 6: Discover and fetch all vaults at this block ---
                vaults = discover_all_vaults(self._contract, block_hash=block_hash)

                if len(vaults) == 0:
                    log.warning(
                        "No vaults discovered at block %d — will retry next poll. "
                        "Check that contract_address matches the deployed contract "
                        "and that tusdt_vault.json ABI is up to date.",
                        final_block,
                    )
                    time.sleep(POLL_INTERVAL)
                    continue

                # --- Step 7: Build rows directly from vault dicts ---
                # Use the actual current chain time as the snapshot time —
                # this IS the :00 snapshot for this hour, taken live.
                now_dt = datetime.fromtimestamp(
                    substrate.query("Timestamp", "Now").value / 1000, tz=timezone.utc
                )
                snapshot_time_utc = now_dt.strftime("%Y-%m-%d %H:%M:%S")

                new_rows: List[Dict[str, Any]] = []
                for v in vaults:
                    new_rows.append(
                        {
                            "snapshot_hour": current_hour,
                            "snapshot_time_utc": snapshot_time_utc,
                            "block_number": final_block,
                            "vault_owner": v.get("owner"),
                            "vault_id": v.get("id"),
                            "collateral_balance": v.get("collateral_balance"),
                            "borrowed_token_balance": v.get("borrowed_token_balance"),
                            "created_at": v.get("created_at"),
                            "last_interest_accrued_at": v.get(
                                "last_interest_accrued_at"
                            ),
                        }
                    )

                # --- Step 8: Append to data.csv ---
                write_header = not data_csv_path.exists()
                with open(data_csv_path, "a", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                    if write_header:
                        writer.writeheader()
                    writer.writerows(new_rows)

                log.info(
                    "Appended %d vault rows for hour %d on %s (block %d).",
                    len(new_rows),
                    current_hour,
                    date_str,
                    final_block,
                )

                # --- Step 9: Sleep ---
                time.sleep(POLL_INTERVAL)

            except Exception as exc:
                log.error("Ground-truth poll error: %s", exc, exc_info=True)
                # Reset connection on unexpected errors so next poll starts
                # fresh rather than reusing a potentially broken connection.
                self._substrate = None
                self._contract = None
                time.sleep(POLL_INTERVAL)
