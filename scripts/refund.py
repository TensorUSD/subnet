"""
Catch up historical finalized auctions and claim pending refunds for the miner.

By default this scans a recent block window up to the current chain head, then
applies the same miner finalized-event flow:
- if winner is this miner: skip
- if winner is someone else: check bid and call withdraw_refund when not withdrawn

COLDKEY_PASSWORD is read from .env automatically.

Usage:
    uv run scripts/refund.py \
        --netuid <NETUID> \
        --subtensor.network <NETWORK> \
        --wallet.name <WALLET_NAME> \
        --wallet.hotkey <HOTKEY_NAME> \
        [--auction_contract.address <AUCTION_ADDRESS>] \
        [--rpc <RPC_ENDPOINT>] \
        [--lookback_block <LOOKBACK_BLOCK> || --start_block <START_BLOCK> --end_block <END_BLOCK>] \
        --logging.info

Mandatory:
-> --netuid, --subtensor.network, --wallet.name, --wallet.hotkey
-> --auction_contract.address (or AUCTION_CONTRACT_ADDRESS in .env)

Range alternatives (choose one):
-> --lookback_block <LOOKBACK_BLOCK>
-> --start_block <START_BLOCK> --end_block <END_BLOCK>

If no range flags are provided, default is:
-> --lookback_block 3000

Optional:
-> --rpc <RPC_ENDPOINT> to override the endpoint from --subtensor.network
-> --logging.info for info-level logs

Example:
    uv run scripts/refund.py \
        --netuid <NETUID> \
        --subtensor.network <NETWORK> \
        --wallet.name <WALLET_NAME> \
        --wallet.hotkey <HOTKEY_NAME> \
        --start_block 6737000 \
        --end_block 6740000 \
        --logging.info
"""

import argparse
import asyncio
import os
import logging

import bittensor as bt
from dotenv import load_dotenv

from tensorusd.auction.contract import TensorUSDAuctionContract, create_substrate_interface
from tensorusd.auction.event_listener import AuctionEventListener
from tensorusd.miner.auction_manager import MinerAuctionManager

logging.getLogger("bittensor").propagate = False
load_dotenv()


def build_config() -> bt.Config:
    parser = argparse.ArgumentParser(
        description="Scan historical finalized auctions and claim pending refunds"
    )
    bt.Wallet.add_args(parser)
    bt.Subtensor.add_args(parser)
    bt.logging.add_args(parser)

    parser.add_argument(
        "--auction_contract.address",
        type=str,
        default=os.getenv("AUCTION_CONTRACT_ADDRESS"),
        required=os.getenv("AUCTION_CONTRACT_ADDRESS") is None,
        help="TensorUSD Auction contract address (SS58).",
    )

    parser.add_argument(
        "--start_block",
        type=int,
        default=None,
        help=(
            "Optional inclusive start block for historical scan. "
            "Use together with --end_block when --lookback_block is not set."
        ),
    )

    parser.add_argument(
        "--end_block",
        type=int,
        default=None,
        help=(
            "Optional inclusive end block for historical scan. "
            "Use together with --start_block when --lookback_block is not set."
        ),
    )

    parser.add_argument(
        "--lookback_block",
        type=int,
        default=None,
        help=(
            "Optional lookback window from current head. "
            "When set, scan range is (current_block - lookback_block, current_block). "
            "If no range flags are provided, defaults to 3000."
        ),
    )

    parser.add_argument(
        "--rpc",
        type=str,
        default="",
        help=(
            "Optional RPC endpoint override. "
            "If empty, uses subtensor.chain_endpoint from --subtensor.network."
        ),
    )

    return bt.Config(parser)


def unlock_wallet(config: bt.Config) -> bt.Wallet:
    coldkey_password = os.getenv("COLDKEY_PASSWORD")
    if not coldkey_password:
        raise RuntimeError("COLDKEY_PASSWORD not set in .env")

    wallet = bt.Wallet(config=config)
    wallet.coldkey_file.save_password_to_env(coldkey_password)
    wallet.unlock_coldkey()
    bt.logging.info(f"Wallet ready - coldkey: {wallet.coldkey.ss58_address}")
    return wallet


async def main():
    config = build_config()
    bt.logging(config=config)

    try:
        wallet = unlock_wallet(config)
    except Exception as e:
        bt.logging.error(f"Failed to unlock wallet: {e}")
        return

    subtensor = bt.Subtensor(config=config)
    chain_endpoint = config.rpc.strip() if config.rpc else ""
    if not chain_endpoint:
        chain_endpoint = subtensor.chain_endpoint
    bt.logging.info(f"Using chain endpoint: {chain_endpoint}")

    substrate = create_substrate_interface(chain_endpoint)

    auction_contract = TensorUSDAuctionContract(
        substrate=substrate,
        contract_address=config.auction_contract.address,
        metadata_path="tensorusd/abis/tusdt_auction.json",
        wallet=wallet,
    )

    event_listener = AuctionEventListener(
        substrate=substrate,
        contract_address=config.auction_contract.address,
        metadata_path="tensorusd/abis/tusdt_auction.json",
        callback=lambda _event: None,
    )

    manager = MinerAuctionManager(
        auction_contract=auction_contract,
        vault_contract=None,
        strategy=None,
        wallet=wallet,
    )

    effective_lookback_block = config.lookback_block
    if (
        effective_lookback_block is None
        and config.start_block is None
        and config.end_block is None
    ):
        effective_lookback_block = 3000

    if effective_lookback_block is not None and effective_lookback_block < 0:
        bt.logging.error("--lookback_block must be >= 0")
        return

    current_block = auction_contract.get_current_block()

    if effective_lookback_block is not None:
        if config.start_block is not None or config.end_block is not None:
            bt.logging.warning(
                "--lookback_block is set; ignoring --start_block/--end_block and using current-relative range"
            )
        end_block = current_block
        start_block = max(0, current_block - effective_lookback_block)
        bt.logging.info(
            "Using current-relative window: "
            f"start_block={start_block}, end_block={end_block}, "
            f"lookback_block={effective_lookback_block}"
        )
    else:
        if config.start_block is None or config.end_block is None:
            bt.logging.error(
                "Provide both --start_block and --end_block, or use --lookback_block"
            )
            return
        start_block = config.start_block
        end_block = config.end_block

    if start_block > end_block:
        bt.logging.warning(
            f"start_block ({start_block}) > end_block ({end_block}); clamping start_block to end_block"
        )
        start_block = end_block

    if effective_lookback_block is None and config.start_block is None:
        bt.logging.info(
            "Historical refund sync range resolved from default lookback"
        )

    bt.logging.info(
        "Running historical refund sync "
        f"for blocks {start_block}..{end_block}"
    )

    await manager.sync_historical_finalized_refunds(
        event_listener=event_listener,
        start_block=start_block,
        end_block=end_block,
    )


if __name__ == "__main__":
    asyncio.run(main())
