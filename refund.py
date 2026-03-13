"""
Check whether bids for a given auction ID have been refunded.

COLDKEY_PASSWORD is read from .env automatically.

Usage:
    uv run refund.py \
        --netuid 421 \
        --subtensor.network test \
        --wallet.name test \
        --wallet.hotkey default \
        --logging.info
"""

import asyncio
import argparse
import os

import bittensor as bt
from dotenv import load_dotenv
import logging
logging.getLogger("bittensor").propagate = False

from tensorusd.auction.types import AuctionEvent, AuctionEventType
from tensorusd.auction.contract import TensorUSDAuctionContract, create_substrate_interface
from tensorusd.miner.auction_manager import MinerAuctionManager

load_dotenv()

# Hardcode the auction ID to check
AUCTION_ID = 9


def build_config() -> bt.Config:
    parser = argparse.ArgumentParser(description="Check auction bid refund status")
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

    return bt.Config(parser)


async def main():
    config = build_config()
    bt.logging(config=config)

    # Unlock wallet — coldkey is the bidder, hotkey is the signing keypair
    coldkey_password = os.getenv("COLDKEY_PASSWORD")
    if not coldkey_password:
        bt.logging.error("COLDKEY_PASSWORD not set in .env. Exiting.")
        return
    wallet = bt.Wallet(config=config)
    wallet.coldkey_file.save_password_to_env(coldkey_password)
    wallet.unlock_coldkey()
    bt.logging.info(f"Wallet ready — coldkey: {wallet.coldkey.ss58_address}")

    # Connect to chain
    subtensor = bt.Subtensor(config=config)
    substrate = create_substrate_interface(subtensor.chain_endpoint)

    auction_contract = TensorUSDAuctionContract(
        substrate=substrate,
        contract_address=config.auction_contract.address,
        metadata_path="tensorusd/abis/tusdt_auction.json",
        wallet=wallet,
    )

    # Pre-checks: auction must exist and be finalized before doing anything
    bt.logging.info(f"Fetching auction {AUCTION_ID} from chain ...")
    auction = auction_contract.get_auction(AUCTION_ID)

    if auction is None:
        bt.logging.error(f"Auction {AUCTION_ID} not found on chain. Exiting.")
        return

    if not auction.is_finalized:
        bt.logging.warning(
            f"Auction {AUCTION_ID} is not finalized yet. "
            f"highest_bid={auction.highest_bid}, "
            f"highest_bidder={auction.highest_bidder}. Exiting."
        )
        return

    # vault_contract/strategy/tusdt not needed for handle_auction_finalized
    manager = MinerAuctionManager(
        auction_contract=auction_contract,
        vault_contract=None,
        strategy=None,
        wallet=wallet,
    )

    # Build event using real on-chain winner data
    event = AuctionEvent(
        event_type=AuctionEventType.FINALIZED,
        block_number=0,
        auction_id=AUCTION_ID,
        winner=auction.highest_bidder,
        highest_bid=auction.highest_bid,
    )

    bt.logging.info(f"Checking refund status for auction_id={AUCTION_ID} ...")
    await manager.handle_auction_finalized(event)


if __name__ == "__main__":
    asyncio.run(main())