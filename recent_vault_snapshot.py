#!/usr/bin/env python3
"""
recent_vault_snapshot.py — Fetch all vault info from the most-recent :00 mark.

What it does:
  1. Connect to the chain
  2. Get the current blockchain timestamp
  3. Round it *down* to the previous hour's :00 (e.g. 17:05 → 17:00)
  4. Find the block closest to that :00 timestamp
  5. Discover all vaults at that block
  6. Fetch each vault's details
  7. Output as CSV

Usage:
    # Default: finney network, CSV to stdout
    python recent_vault_snapshot.py

    # Save to file
    python recent_vault_snapshot.py --output vaults_latest.csv

    # Custom network / endpoint
    python recent_vault_snapshot.py --network testnet
    python recent_vault_snapshot.py --endpoint wss://entrypoint-finance.opentensor.ai:443

    # Override vault contract (default is the production address)
    python recent_vault_snapshot.py --vault-contract-address 5...
"""

import argparse
import csv
import math
import sys
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import bittensor as bt

from substrateinterface.contracts import (
    ContractMetadata,
    ContractInstance,
)

from tensorusd.common.contract import (
    TensorUSDVaultContract,
    create_substrate_interface,
    Vault,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PAGE_SIZE = 10
"""Number of vaults returned per page by get_all_vaults."""

DEFAULT_RPC_ENDPOINTS = {
    "finney": [
        "wss://entrypoint-finney.opentensor.ai:443",
        "wss://finney.opentensor.ai:443",
        "wss://entrypoint-finance.opentensor.ai:443",
    ],
    "testnet": ["wss://test.finney.opentensor.ai:443"],
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch all vault info from the most-recent :00 mark."
    )

    parser.add_argument(
        "--subtensor.network",
        "--network",
        type=str,
        default="finney",
        help="Bittensor network name (e.g. finney, testnet).",
    )
    parser.add_argument(
        "--subtensor.chain_endpoint",
        "--endpoint",
        type=str,
        default=None,
        help="Substrate RPC endpoint (overrides --network).",
    )
    parser.add_argument(
        "--vault-contract-address",
        type=str,
        default="5H8nuGvHJdNXuSWtquddcGQDgAvK4vEvXmvKwU6o4cCmvfPu",
        help="Vault contract SS58 address.",
    )
    parser.add_argument(
        "--wallet.name",
        type=str,
        default="tusd-test-vali",
        help="Bittensor wallet name.",
    )
    parser.add_argument(
        "--wallet.hotkey",
        type=str,
        default="default",
        help="Bittensor hotkey name.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output CSV file path (default: stdout).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=True,
        help="Print progress to stderr (default: on).",
    )

    args = parser.parse_args()

    network = getattr(args, "subtensor.network") or "finney"
    endpoint = getattr(args, "subtensor.chain_endpoint") or None

    if endpoint and endpoint not in {"", "None"}:
        args.rpc_endpoint = endpoint
    else:
        args.rpc_endpoint = DEFAULT_RPC_ENDPOINTS.get(
            network, ["wss://test.finney.opentensor.ai:443"]
        )[0]
    args.network = network
    args.wallet_name = getattr(args, "wallet.name") or "tusd-test-vali"
    args.wallet_hotkey = getattr(args, "wallet.hotkey") or "default"

    return args


# ---------------------------------------------------------------------------
# Logging helpers (always to stderr so stdout is clean CSV)
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[recent_vault_snapshot] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Block / timestamp helpers (adapted from get_vault_info.py)
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
        log(f"Error querying timestamp at block {block_number}: {e}")
        return 0


def _estimate_block_at_timestamp(
    substrate, target_timestamp_ms: int, max_block: Optional[int] = None
) -> int:
    """
    Estimate the block number whose timestamp is closest to *target_timestamp_ms*.
    """
    current_block = substrate.get_block_number(None)
    current_timestamp = substrate.query("Timestamp", "Now").value

    if not current_timestamp or current_timestamp <= 0:
        raise RuntimeError("Failed to query current chain timestamp.")

    if max_block is not None and current_block > max_block:
        current_block = max_block
        bh = substrate.get_block_hash(max_block)
        current_timestamp = substrate.query(
            "Timestamp", "Now", block_hash=bh
        ).value

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
    """Find nearest block that is still served by the RPC endpoint."""
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


def _is_ws_url(value: str) -> bool:
    return urlparse(value).scheme in {"ws", "wss"}


def create_substrate_with_fallback(network: str, preferred_endpoint: str):
    """Create a substrate interface with fallback endpoints."""
    endpoints = []
    if preferred_endpoint and _is_ws_url(preferred_endpoint):
        endpoints.append(preferred_endpoint)
    endpoints.extend(DEFAULT_RPC_ENDPOINTS.get(network, []))

    seen = set()
    unique_endpoints = []
    for endpoint in endpoints:
        if endpoint not in seen:
            seen.add(endpoint)
            unique_endpoints.append(endpoint)

    last_error = None
    for endpoint in unique_endpoints:
        try:
            log(f"Connecting to substrate endpoint: {endpoint}")
            return create_substrate_interface(endpoint)
        except Exception as exc:
            last_error = exc
            log(f"Failed to connect to {endpoint}: {exc}")

    raise RuntimeError(
        f"Unable to connect to any substrate endpoint for network '{network}'."
    ) from last_error


# ---------------------------------------------------------------------------
# Vault discovery (direct calls with verbose logging)
# ---------------------------------------------------------------------------


def _get_total_vaults_count(
    contract: TensorUSDVaultContract, block_hash: Optional[bytes] = None
) -> int:
    """Call get_total_vaults_count on the vault contract."""
    try:
        result = contract.contract.read(
            keypair=contract.wallet.hotkey,
            method="get_total_vaults_count",
            block_hash=block_hash,
        )
        data = result.contract_result_data.value_object
        log(f"Raw response for get_total_vaults_count: {data}")
        if data and data[0] == "Ok":
            return data[1].value
        return 0
    except Exception as e:
        log(f"Contract call get_total_vaults_count failed: {e}")
        return 0


def _get_all_vaults_page(
    contract: TensorUSDVaultContract,
    page: int,
    block_hash: Optional[bytes] = None,
) -> List[Tuple[str, int]]:
    """
    Call get_all_vaults(page) and return list of (owner_ss58, vault_id).
    """
    try:
        result = contract.contract.read(
            keypair=contract.wallet.hotkey,
            method="get_all_vaults",
            args={"page": page},
            block_hash=block_hash,
        )
        data = result.contract_result_data.value_object
        log(f"Raw response for get_all_vaults(page={page}): {data}")
        vaults: List[Tuple[str, int]] = []

        if data and data[0] == "Ok" and data[1]:
            raw_list = data[1].value
            if isinstance(raw_list, list):
                for entry in raw_list:
                    if isinstance(entry, dict):
                        if "Ok" in entry:
                            inner = entry["Ok"]
                            if isinstance(inner, (list, tuple)) and len(inner) >= 2:
                                vaults.append((inner[0], int(inner[1])))
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
                            # e.g. {'id': 0, 'owner': '5...', ...}
                            if "owner" in entry and "vault_id" in entry:
                                vaults.append((entry["owner"], int(entry["vault_id"])))
                            elif "id" in entry and "owner" in entry:
                                vaults.append((entry["owner"], int(entry["id"])))

        return vaults
    except Exception as e:
        log(f"get_all_vaults(page={page}) failed: {e}")
        return []


def discover_all_vaults(
    contract: TensorUSDVaultContract,
    block_hash: Optional[bytes] = None,
) -> List[Tuple[str, int]]:
    """Discover all vaults by paginating through get_all_vaults."""
    total = _get_total_vaults_count(contract, block_hash=block_hash)
    log(f"Total vaults count: {total}")
    if total == 0:
        log("No vaults found (count = 0).")
        return []

    total_pages = math.ceil(total / PAGE_SIZE)
    log(f"Fetching {total_pages} pages of vaults...")
    all_vaults: List[Tuple[str, int]] = []

    for page in range(total_pages):
        log(f"Getting page {page}/{total_pages}...")
        page_vaults = _get_all_vaults_page(contract, page, block_hash=block_hash)
        log(f"Page {page} returned {len(page_vaults)} vaults")
        all_vaults.extend(page_vaults)

    # Deduplicate
    seen = set()
    unique: List[Tuple[str, int]] = []
    for owner, vid in all_vaults:
        key = (owner, vid)
        if key not in seen:
            seen.add(key)
            unique.append(key)

    log(f"Total unique vaults discovered: {len(unique)}")
    return unique


def _get_vault_at_block(
    contract: TensorUSDVaultContract,
    owner: str,
    vault_id: int,
    block_hash: Optional[bytes] = None,
) -> Optional[Vault]:
    """Fetch a single vault's details at a specific block."""
    try:
        result = contract.contract.read(
            keypair=contract.wallet.hotkey,
            method="get_vault",
            args={"owner": owner, "vault_id": vault_id},
            block_hash=block_hash,
        )
        data = result.contract_result_data.value_object
        log(f"Raw vault data for {owner}/{vault_id}: {data}")
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
        log(f"Error getting vault {owner}/{vault_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()

    # --- Connect ---
    log(f"Network: {args.network}, endpoint: {args.rpc_endpoint}")
    substrate = create_substrate_with_fallback(args.network, args.rpc_endpoint)
    log("Connection established.")

    # --- Wallet ---
    wallet = bt.Wallet(
        name=args.wallet_name,
        hotkey=args.wallet_hotkey,
    )
    log(
        f"Wallet: {wallet.name}/{wallet.hotkey_str} "
        f"(hotkey: {wallet.hotkey.ss58_address})"
    )
    if not wallet.hotkey or not getattr(wallet.hotkey, "ss58_address", None):
        log("ERROR: Wallet hotkey not available.")
        sys.exit(1)

    # --- Vault contract ---
    vault_contract = TensorUSDVaultContract(
        substrate=substrate,
        contract_address=args.vault_contract_address,
        metadata_path="tensorusd/common/abis/tusdt_vault.json",
        wallet=wallet,
    )
    log(f"Vault contract: {args.vault_contract_address}")

    # --- Get current chain time ---
    try:
        current_ts = substrate.query("Timestamp", "Now").value
        current_block = substrate.get_block_number(None)
    except Exception as e:
        log(f"ERROR: Failed to query chain state: {e}")
        sys.exit(1)

    log(f"Current chain block: {current_block}, timestamp: {current_ts}")
    log(f"Current UTC: {datetime.fromtimestamp(current_ts / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")

    # --- Round down to the previous hour's :00 ---
    current_seconds = current_ts // 1000
    previous_hour_seconds = (current_seconds // 3600) * 3600
    target_timestamp_ms = previous_hour_seconds * 1000

    target_dt = datetime.fromtimestamp(previous_hour_seconds, tz=timezone.utc)
    log(f"Target :00 timestamp: {target_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC "
        f"(epoch ms: {target_timestamp_ms})")

    # --- Find the block at that :00 ---
    log("Estimating block for target timestamp...")
    try:
        estimate = _estimate_block_at_timestamp(substrate, target_timestamp_ms)
    except Exception as e:
        log(f"ERROR estimating block: {e}")
        sys.exit(1)
    log(f"Initial block estimate: {estimate}")

    refined = _refine_block_estimate(substrate, target_timestamp_ms, estimate)
    log(f"Refined block estimate: {refined}")

    final_block = _find_nearest_available_block(substrate, refined)
    log(f"Final available block: {final_block}")

    # --- Get the block hash (with error handling) ---
    try:
        block_hash = substrate.get_block_hash(final_block)
    except Exception as e:
        log(f"ERROR getting block hash for block {final_block}: {e}")
        # Fallback: try current block instead
        log("Falling back to current block...")
        try:
            final_block = substrate.get_block_number(None)
            block_hash = substrate.get_block_hash(final_block)
        except Exception as e2:
            log(f"ERROR also failed to get current block hash: {e2}")
            sys.exit(1)

    if block_hash is None:
        log(f"WARNING: Block hash is None for block {final_block}. "
            "This likely means the block is pruned on this endpoint.")
        log("Falling back to current block...")
        try:
            final_block = substrate.get_block_number(None)
            block_hash = substrate.get_block_hash(final_block)
        except Exception as e:
            log(f"ERROR getting current block: {e}")
            sys.exit(1)
        if block_hash is None:
            log("ERROR: Cannot get any block hash. Exiting.")
            sys.exit(1)

    log(f"Block hash: {block_hash[:16]}...")

    actual_ts = _get_chain_timestamp_at_block(substrate, final_block)
    actual_dt = datetime.fromtimestamp(actual_ts / 1000, tz=timezone.utc)
    log(f"Actual timestamp at block {final_block}: "
        f"{actual_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC "
        f"(epoch ms: {actual_ts})")
    diff_seconds = (actual_ts - target_timestamp_ms) / 1000
    log(f"Difference from target :00: {diff_seconds:+.1f} seconds")

    # --- Discover all vaults at this block ---
    log(f"Discovering vaults at block {final_block}...")
    vault_keys = discover_all_vaults(vault_contract, block_hash=block_hash)
    log(f"Discovered {len(vault_keys)} vault(s).")

    # --- If historical block returned 0 vaults due to pruning, fall back to current block ---
    if len(vault_keys) == 0:
        log("WARNING: No vaults found at historical block (likely RPC state pruning).")
        log("Falling back to current block...")
        try:
            current_block = substrate.get_block_number(None)
            current_hash = substrate.get_block_hash(current_block)
            if current_hash:
                log(f"Trying current block {current_block} with hash {current_hash[:16]}...")
                vault_keys = discover_all_vaults(vault_contract, block_hash=current_hash)
                log(f"Current block vaults discovered: {len(vault_keys)}")
                if len(vault_keys) > 0:
                    final_block = current_block
                    block_hash = current_hash
                    # Update timestamps for the current block
                    actual_ts = _get_chain_timestamp_at_block(substrate, final_block)
                    actual_dt = datetime.fromtimestamp(actual_ts / 1000, tz=timezone.utc) if actual_ts else target_dt
                    log(f"Using current block {final_block} (ts: {actual_dt.strftime('%Y-%m-%d %H:%M:%S') if actual_ts else 'unknown'})")
        except Exception as e:
            log(f"ERROR falling back to current block: {e}")

    if len(vault_keys) == 0:
        log("ERROR: Could not discover any vaults at any block. Exiting.")
        sys.exit(1)

    # --- Fetch vault details ---
    rows: List[Dict[str, Any]] = []
    for idx, (owner, vid) in enumerate(vault_keys):
        log(f"Fetching vault {idx + 1}/{len(vault_keys)}: owner={owner}, vault_id={vid}")
        vault = _get_vault_at_block(
            vault_contract, owner, vid, block_hash=block_hash
        )
        if vault is None:
            log(f"  WARNING: vault not found at this block: {owner}/{vid}")
            continue

        rows.append({
            "snapshot_time_utc": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "block_number": final_block,
            "vault_owner": vault.owner,
            "vault_id": vault.id,
            "collateral_balance": vault.collateral_balance,
            "borrowed_token_balance": vault.borrowed_token_balance,
            "created_at": vault.created_at,
            "last_interest_accrued_at": vault.last_interest_accrued_at,
        })
        log(f"  OK — collateral={vault.collateral_balance}, "
            f"borrowed={vault.borrowed_token_balance}")

    # --- Output ---
    fieldnames = [
        "snapshot_time_utc",
        "block_number",
        "vault_owner",
        "vault_id",
        "collateral_balance",
        "borrowed_token_balance",
        "created_at",
        "last_interest_accrued_at",
    ]

    if args.output:
        fh = open(args.output, "w", newline="")
        log(f"Writing {len(rows)} row(s) to {args.output}")
    else:
        fh = sys.stdout

    writer = csv.DictWriter(fh, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

    if args.output:
        fh.close()

    log("Done.")

    # --- Cleanup ---
    substrate.close()


if __name__ == "__main__":
    main()