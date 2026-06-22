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
    ],
    "testnet": ["wss://test.finney.opentensor.ai:443"],
}

# Ground-truth directory (same default as tensorusd/auth/config.py)
GROUND_TRUTH_DIR = Path(os.environ.get("TENSORUSD_GROUND_TRUTH_DIR", "ground-truth"))

# How often to check if it's time to collect (seconds)
POLL_INTERVAL = 60


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def _is_ws_url(value: str) -> bool:
    return urlparse(value).scheme in {"ws", "wss"}


def _create_substrate_with_fallback(network: str, preferred_endpoint: str):
    """Create a substrate interface with fallback endpoints."""
    endpoints = []
    if preferred_endpoint and _is_ws_url(preferred_endpoint):
        endpoints.append(preferred_endpoint)
    endpoints.extend(DEFAULT_RPC_ENDPOINTS.get(network, []))

    seen = set()
    unique_endpoints = []
    for ep in endpoints:
        if ep not in seen:
            seen.add(ep)
            unique_endpoints.append(ep)

    last_error = None
    for ep in unique_endpoints:
        try:
            log.info("Connecting to substrate endpoint: %s", ep)
            return create_substrate_interface(ep)
        except Exception as exc:
            last_error = exc
            log.warning("Failed to connect to %s: %s", ep, exc)

    raise RuntimeError(
        f"Unable to connect to any substrate endpoint for network '{network}'."
    ) from last_error


# ---------------------------------------------------------------------------
# Block / timestamp helpers
# ---------------------------------------------------------------------------


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
    """Estimate the block number whose timestamp is closest to target_timestamp_ms."""
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
    """Refine block estimate ±1000 blocks to get closest to target timestamp."""
    best_diff = float("inf")
    best_block = initial_estimate

    for offset in range(-1000, 1001, 50):
        blk = max(initial_estimate + offset, 1)
        try:
            ts = _get_chain_timestamp_at_block(substrate, blk)
        except Exception:
            continue
        if ts == 0:
            continue
        diff = abs(ts - target_timestamp_ms)
        if diff < best_diff:
            best_diff = diff
            best_block = blk

    return best_block


def _find_nearest_available_block(
    substrate, target_block: int, search_window: int = 2000
) -> int:
    """Find the nearest block that is still served by the RPC endpoint."""
    candidates = [target_block]
    for offset in range(1, search_window + 1):
        candidates.extend([target_block - offset, target_block + offset])

    for blk in candidates:
        if blk < 1:
            continue
        try:
            bh = substrate.get_block_hash(blk)
            if bh is None:
                continue
            ts = substrate.query("Timestamp", "Now", block_hash=bh).value
            if ts:
                return blk
        except Exception:
            continue

    return target_block


# ---------------------------------------------------------------------------
# Vault discovery helpers
# ---------------------------------------------------------------------------


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
) -> Tuple[List[Tuple[str, int]], bool]:
    """
    Call get_all_vaults(page) and return (vaults, success).

    Returns ([], False) on any decode/RPC error so the caller can stop
    paginating early rather than burning through pages that don't exist
    at a historical block.
    """
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
        return [], False

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
    return vaults, True


def discover_all_vaults(
    contract: TensorUSDVaultContract,
    block_hash: Optional[bytes] = None,
) -> List[Tuple[str, int]]:
    """
    Discover all vaults by paginating through get_all_vaults.

    Uses get_total_vaults_count as an upper bound on pages, but stops
    early on decode failure — this handles historical blocks where the
    encoded state is smaller than the current total implies, as well as
    ABI mismatches that would otherwise spam warnings for every page.
    """
    total = _get_total_vaults_count(contract, block_hash=block_hash)
    if total == 0:
        return []

    total_pages = math.ceil(total / PAGE_SIZE)
    all_vaults: List[Tuple[str, int]] = []

    for page in range(total_pages):
        page_vaults, ok = _get_all_vaults_page(contract, page, block_hash=block_hash)
        if not ok:
            # Decode failure — stop rather than spamming errors for every
            # remaining page. This covers both historical-block state gaps
            # and ABI mismatches.
            log.info(
                "Stopping pagination at page %d (decode failed — "
                "historical block may have fewer vaults than current total, "
                "or contract address / ABI mismatch).",
                page,
            )
            break
        all_vaults.extend(page_vaults)
        if len(page_vaults) < PAGE_SIZE:
            # Natural last page — no point querying further.
            break

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


# ---------------------------------------------------------------------------
# Hour / target helpers
# ---------------------------------------------------------------------------


def _get_target_hour_from_chain_timestamp(substrate) -> Tuple[str, int, int]:
    """
    Determine the most recent completed :00 hour using the **chain timestamp**.

    Returns:
        (date_str, hour, target_timestamp_ms)

    Example: if chain time is 14:05 UTC → ("2026-06-19", 14, 14:00:00 in ms)
    """
    current_ts = substrate.query("Timestamp", "Now").value  # ms
    current_seconds = current_ts // 1000
    # Round down to the previous hour's :00
    previous_hour_seconds = (current_seconds // 3600) * 3600
    target_timestamp_ms = previous_hour_seconds * 1000

    dt = datetime.fromtimestamp(previous_hour_seconds, tz=timezone.utc)
    date_str = dt.strftime("%Y-%m-%d")
    hour = dt.hour  # 0..23
    return date_str, hour, target_timestamp_ms


# ---------------------------------------------------------------------------
# GroundTruthCollector class
# ---------------------------------------------------------------------------


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

    def __init__(
        self,
        wallet: bt.Wallet,
        network: str = "testnet",
        # FIX: use the same contract address as recent_vault_snapshot.py
        contract_address: str = "5H8nuGvHJdNXuSWtquddcGQDgAvK4vEvXmvKwU6o4cCmvfPu",
    ) -> None:
        """
        Args:
            wallet: Bittensor wallet (hotkey used for read-only queries).
            network: Substrate network name (``"testnet"`` or ``"finney"``).
            contract_address: SS58 address of the vault contract.
                              Must match the address used by recent_vault_snapshot.py
                              so that the same ABI decodes correctly.
        """
        self._wallet = wallet
        self._network = network
        self._contract_address = contract_address
        self._stop = threading.Event()

        # Separate SubstrateInterface — not the validator's bt.Subtensor
        self._substrate = None
        self._contract = None
        self._metadata_path = "tensorusd/common/abis/tusdt_vault.json"

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
                self._network,
                DEFAULT_RPC_ENDPOINTS.get(self._network, [None])[0],
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

    def _poll_loop(self) -> None:
        """
        Background poll loop — runs once per minute.

        Each iteration:
          1. Query chain timestamp and round down to most recent :00.
          2. Check if ``data.csv`` for today already has a row for that hour.
          3. If not → find the block at that :00 → snapshot all vaults.
          4. If the historical block decode fails, reconnect fresh and fall
             back to the current block so we still get a snapshot.
          5. When ``data.csv`` has 24 rows → build ``ground-truth.csv``.
        """
        log.info(
            "Ground-truth poll loop started (incremental mode, chain-ts based)."
        )

        while not self._stop.is_set():
            try:
                # --- Ensure connection ---
                if self._substrate is None:
                    if not self._ensure_connection():
                        time.sleep(POLL_INTERVAL)
                        continue

                substrate = self._substrate

                # --- Step 1: Determine the target hour via chain timestamp ---
                date_str, target_hour, target_timestamp_ms = (
                    _get_target_hour_from_chain_timestamp(substrate)
                )

                data_dir = GROUND_TRUTH_DIR / date_str
                data_csv_path = data_dir / "data.csv"
                ground_truth_csv = data_dir / "ground-truth.csv"

                # --- Step 2: Check if today is already done ---
                if ground_truth_csv.exists():
                    log.info(
                        "Ground-truth already exists for %s — skipping.",
                        date_str,
                    )
                    time.sleep(POLL_INTERVAL)
                    continue

                # --- Step 3: Read existing data.csv to see what's been collected ---
                existing_hours: set[int] = set()
                if data_csv_path.exists():
                    with open(data_csv_path, newline="") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            try:
                                existing_hours.add(
                                    int(row.get("snapshot_hour", -1))
                                )
                            except (ValueError, TypeError):
                                pass

                # --- Step 4: Skip if this hour is already collected ---
                if target_hour in existing_hours:
                    time.sleep(POLL_INTERVAL)
                    continue

                # --- Step 5: Find the block closest to the target :00 ---
                data_dir.mkdir(parents=True, exist_ok=True)

                log.info(
                    "Collecting hour %d for %s (chain ts target=%d).",
                    target_hour,
                    date_str,
                    target_timestamp_ms,
                )

                try:
                    estimate = _estimate_block_at_timestamp(
                        substrate, target_timestamp_ms
                    )
                except RuntimeError as e:
                    log.warning("Block estimation failed: %s", e)
                    time.sleep(POLL_INTERVAL)
                    continue

                refined = _refine_block_estimate(
                    substrate, target_timestamp_ms, estimate
                )
                final_block = _find_nearest_available_block(substrate, refined)
                log.info(
                    "Target :00 block: estimate=%d  refined=%d  final=%d",
                    estimate,
                    refined,
                    final_block,
                )

                # Get the block hash
                try:
                    block_hash = substrate.get_block_hash(final_block)
                except Exception as e:
                    log.warning(
                        "Failed to get block hash for %d: %s", final_block, e
                    )
                    block_hash = None

                if block_hash is None:
                    log.warning(
                        "Block hash is None for block %d (likely pruned) — "
                        "falling back to current block.",
                        final_block,
                    )
                    current_block = substrate.get_block_number(None)
                    block_hash = substrate.get_block_hash(current_block)
                    if block_hash is None:
                        log.warning("Cannot get any block hash — retrying later.")
                        time.sleep(POLL_INTERVAL)
                        continue
                    final_block = current_block
                    log.info("Fell back to current block %d.", final_block)

                actual_ts = _get_chain_timestamp_at_block(substrate, final_block)
                if actual_ts:
                    actual_dt = datetime.fromtimestamp(
                        actual_ts / 1000, tz=timezone.utc
                    )
                    diff_seconds = (actual_ts - target_timestamp_ms) / 1000
                    log.info(
                        "Actual block %d timestamp: %s (diff: %+.1f s)",
                        final_block,
                        actual_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        diff_seconds,
                    )

                # --- Step 6: Discover and fetch all vaults at this block ---
                vault_keys = discover_all_vaults(
                    self._contract, block_hash=block_hash
                )

                # If the historical block returned 0 vaults (pruning or decode
                # failure), reconnect fresh and try the current block.
                # A fresh connection is important — reusing a connection that
                # has been used for historical queries can leave the substrate
                # client in a confused state, causing decode failures even on
                # the current block.
                if len(vault_keys) == 0:
                    log.warning(
                        "No vaults at historical block %d — reconnecting and "
                        "trying current block.",
                        final_block,
                    )
                    if not self._ensure_connection():
                        log.warning("Reconnect failed — retrying next poll.")
                        time.sleep(POLL_INTERVAL)
                        continue

                    substrate = self._substrate  # fresh connection
                    current_block = substrate.get_block_number(None)
                    current_hash = substrate.get_block_hash(current_block)
                    if current_hash:
                        vault_keys = discover_all_vaults(
                            self._contract, block_hash=current_hash
                        )
                        if vault_keys:
                            final_block = current_block
                            block_hash = current_hash
                            log.info(
                                "Using current block %d (%d vaults found).",
                                final_block,
                                len(vault_keys),
                            )

                if len(vault_keys) == 0:
                    log.warning(
                        "No vaults discovered at any block — will retry next poll. "
                        "Check that contract_address matches the deployed contract "
                        "and that tusdt_vault.json ABI is up to date."
                    )
                    time.sleep(POLL_INTERVAL)
                    continue

                # --- Step 7: Fetch details for each vault ---
                new_rows: List[Dict[str, Any]] = []
                for owner, vid in vault_keys:
                    v = _get_vault_at_block(
                        self._contract,
                        owner=owner,
                        vault_id=vid,
                        block_hash=block_hash,
                    )
                    if v is None:
                        continue

                    snapshot_dt = datetime.fromtimestamp(
                        target_timestamp_ms / 1000, tz=timezone.utc
                    )
                    snapshot_time_utc = snapshot_dt.strftime("%Y-%m-%d %H:%M:%S")

                    new_rows.append({
                        "snapshot_hour": target_hour,
                        "snapshot_time_utc": snapshot_time_utc,
                        "block_number": final_block,
                        "vault_owner": v.owner,
                        "vault_id": v.id,
                        "collateral_balance": v.collateral_balance,
                        "borrowed_token_balance": v.borrowed_token_balance,
                        "created_at": v.created_at,
                        "last_interest_accrued_at": v.last_interest_accrued_at,
                    })

                # --- Step 8: Append to data.csv ---
                if new_rows:
                    fieldnames = [
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

                    write_header = not data_csv_path.exists()
                    with open(data_csv_path, "a", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        if write_header:
                            writer.writeheader()
                        writer.writerows(new_rows)

                    log.info(
                        "Appended %d new rows for %s (hours collected so far: %d).",
                        len(new_rows),
                        date_str,
                        len(existing_hours) + 1,
                    )

                # --- Step 9: Check if all 24 hours are done ---
                collected_hours: set[int] = set()
                if data_csv_path.exists():
                    with open(data_csv_path, newline="") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            try:
                                collected_hours.add(
                                    int(row.get("snapshot_hour", -1))
                                )
                            except (ValueError, TypeError):
                                pass

                if len(collected_hours) >= 24:
                    log.info(
                        "All 24 hours collected for %s — "
                        "building ground-truth.csv.",
                        date_str,
                    )
                    from tensorusd.validator.ground_truth import (
                        generate_ground_truth,
                    )

                    generate_ground_truth(date_str)

                # --- Step 10: Sleep ---
                time.sleep(POLL_INTERVAL)

            except Exception as exc:
                log.error(
                    "Ground-truth poll error: %s", exc, exc_info=True
                )
                # Reset connection on unexpected errors so next poll starts
                # fresh rather than reusing a potentially broken connection.
                self._substrate = None
                self._contract = None
                time.sleep(POLL_INTERVAL)