"""
Auction manager for miners.

Handles auction events and coordinates bidding.
"""

from typing import Optional

import bittensor as bt

from tensorusd.auction.types import (
    AuctionCreatedEvent,
    AuctionFinalizedEvent,
    BidPlacedEvent,
)
from tensorusd.auction.contract import (
    TensorUSDAuctionContract,
    TensorUSDVaultContract,
)
from tensorusd.auction.erc20 import TUSDTContract
from tensorusd.miner.bidding import BiddingStrategy


class MinerAuctionManager:
    """
    Manages active auctions and bidding for a miner.

    Responsibilities:
    - Fetch auction data from chain when needed
    - Calculate and submit bids (using collateral price for profit calculation)
    - Handle rebidding when outbid
    - Ensure token allowance before bidding
    """

    def __init__(
        self,
        auction_contract: TensorUSDAuctionContract,
        vault_contract: TensorUSDVaultContract,
        strategy: BiddingStrategy,
        wallet: bt.Wallet,
        tusdt_contract: Optional[TUSDTContract] = None,
        approval_amount: Optional[int] = None,
    ):
        """
        Initialize auction manager.

        Args:
            auction_contract: Contract interface for auction operations (bidding, fetching auctions)
            vault_contract: Contract interface for vault operations (getting collateral price)
            strategy: Bidding strategy configuration
            wallet: Miner's wallet for signing bid transactions
            tusdt_contract: Optional TUSDT ERC20 contract for allowance checks
            approval_amount: Amount to approve if allowance insufficient (None = max)
        """
        self.auction_contract = auction_contract
        self.vault_contract = vault_contract
        self.strategy = strategy
        self.wallet = wallet
        self.tusdt_contract = tusdt_contract
        self.approval_amount = approval_amount

    def _get_collateral_price(self) -> Optional[int]:
        return self.vault_contract.get_collateral_token_price()

    async def handle_auction_created(self, event: AuctionCreatedEvent):
        """
        Handle new liquidation auction event.

        Fetches auction details and submits initial bid if profitable.

        Args:
            event: AuctionCreated event
        """
        bt.logging.info(
            f"New auction created: auction_id={event.auction_id}, "
            f"vault_owner={event.vault_owner}, vault_id={event.vault_id}"
        )

        # Fetch auction details from chain
        auction = self.auction_contract.get_auction(event.auction_id)
        if auction is None:
            bt.logging.warning(
                f"Could not fetch auction data for auction {event.auction_id}"
            )
            return

        # Get collateral price for profit calculation
        collateral_price = self._get_collateral_price()
        if collateral_price is None:
            bt.logging.warning(
                f"Could not fetch collateral price for auction {event.auction_id}"
            )
            return

        # Calculate initial bid (no existing bids on new auction)
        bid_amount = self.strategy.calculate_bid(
            auction.collateral_balance,
            auction.debt_balance,
            0,
            collateral_price,
        )

        if bid_amount <= 0:
            bt.logging.info(f"Skipping auction {event.auction_id} - not profitable")
            return

        # Submit bid
        tx_hash = await self._submit_bid(event.auction_id, bid_amount)

        if tx_hash:
            bt.logging.success(
                f"Initial bid placed on auction {event.auction_id}: "
                f"amount={bid_amount}, tx={tx_hash}"
            )

    async def handle_bid_placed(self, event: BidPlacedEvent):
        """
        Handle bid placed event.

        If we were outbid, calculate and submit counter-bid.

        Args:
            event: BidPlaced event
        """
        auction_id = event.auction_id
        my_address = self.wallet.coldkey.ss58_address

        # Ignore our own bids
        if event.bidder == my_address:
            bt.logging.info(f"Ignoring own bid on auction {auction_id}")
            return

        # Fetch current auction state from chain
        auction = self.auction_contract.get_auction(auction_id)
        if auction is None:
            bt.logging.warning(f"Could not fetch auction {auction_id}")
            return

        # Skip if auction is finalized
        if auction.is_finalized:
            bt.logging.debug(f"Auction {auction_id} is finalized, skipping")
            return

        # Skip if we're already the highest bidder
        if auction.highest_bidder == my_address:
            bt.logging.debug(f"Already highest bidder on auction {auction_id}")
            return

        # Get collateral price for bid calculation
        collateral_price = self._get_collateral_price()
        if collateral_price is None:
            bt.logging.warning(
                f"Could not fetch collateral price for auction {auction_id}"
            )
            return

        # Calculate new bid based on current highest bid from chain
        new_bid = self.strategy.calculate_bid(
            auction.collateral_balance,
            auction.debt_balance,
            auction.highest_bid,
            collateral_price,
        )

        if new_bid <= 0:
            bt.logging.info(f"Cannot profitably bid on auction {auction_id}")
            return

        # Submit counter-bid
        tx_hash = await self._submit_bid(auction_id, new_bid)

        if tx_hash:
            bt.logging.success(
                f"Counter-bid placed on auction {auction_id}: "
                f"amount={new_bid}, tx={tx_hash}"
            )

    async def handle_auction_finalized(self, event: AuctionFinalizedEvent):
        """
        Handle auction finalized event.

        Log result.

        Args:
            event: AuctionFinalized event
        """
        auction_id = event.auction_id
        my_address = self.wallet.coldkey.ss58_address

        if event.winner == my_address:
            bt.logging.success(
                f"Won auction {auction_id}! winning_bid={event.highest_bid}"
            )
        else:
            bt.logging.info(
                f"Auction {auction_id} finalized. "
                f"winner={event.winner}, "
                f"winning_bid={event.highest_bid}"
            )
            miner_bid = self.auction_contract.get_auction_bid(auction_id, my_address)
            if miner_bid is None:
                bt.logging.warning(
                    f"Could not fetch miner bid for auction {auction_id}, skipping"
                )
            else:
                tx_hash = await self.auction_contract.withdraw_refund(
                    auction_id, miner_bid.id
                )
                if tx_hash:
                    bt.logging.success(
                        f"Refund withdrawn for auction {auction_id}: "
                        f"amount={miner_bid.amount}, tx={tx_hash}"
                    )
                else:
                    bt.logging.error(
                        f"Failed to withdraw refund for auction {auction_id}"
                    )

    async def _submit_bid(self, auction_id: int, bid_amount: int) -> Optional[str]:
        """
        Submit bid transaction to blockchain.

        Checks and ensures sufficient token allowance before placing bid.

        Args:
            auction_id: Auction to bid on
            bid_amount: Bid amount in USDT

        Returns:
            Transaction hash if successful, None otherwise
        """
        try:
            # Check and ensure allowance if TUSDT contract is configured
            if self.tusdt_contract is not None:
                allowance_ok = self.tusdt_contract.ensure_allowance(
                    spender=self.auction_contract.contract_address,
                    required_amount=bid_amount,
                    approval_amount=self.approval_amount,
                )
                if not allowance_ok:
                    bt.logging.error(f"Failed to ensure allowance for bid {bid_amount}")
                    return None

            tx_hash = self.auction_contract.place_bid(
                auction_id=auction_id,
                bid_amount=bid_amount,
                keypair=self.wallet.coldkey,
                hotkey_ss58=self.wallet.hotkey.ss58_address,
            )
            return tx_hash
        except Exception as e:
            bt.logging.error(f"Failed to submit bid for auction {auction_id}: {e}")
            return None

    async def sync_active_auctions(self):
        """
        Sync with active auctions on startup.

        Fetches active auctions from contract and bids on any profitable ones.
        """
        bt.logging.info("Syncing active auctions...")

        active_auctions = self.auction_contract.get_active_auctions()

        if not active_auctions:
            bt.logging.info("No active auctions found")
            return

        bt.logging.info(f"Found {len(active_auctions)} active auctions")

        # Get collateral price once for all auctions
        collateral_price = self._get_collateral_price()
        if collateral_price is None:
            bt.logging.error("Could not fetch collateral price, skipping sync")
            return

        my_address = self.wallet.coldkey.ss58_address

        for auction in active_auctions:
            # Skip if we're already the highest bidder
            if auction.highest_bidder == my_address:
                bt.logging.info(
                    f"Skipping auction {auction.auction_id} - already highest bidder"
                )
                continue

            # Calculate bid (considering current highest bid and collateral price)
            bid_amount = self.strategy.calculate_bid(
                auction.collateral_balance,
                auction.debt_balance,
                auction.highest_bid,
                collateral_price,
            )

            if bid_amount <= 0:
                bt.logging.info(
                    f"Skipping auction {auction.auction_id} - not profitable"
                )
                continue

            # Submit bid
            tx_hash = await self._submit_bid(auction.auction_id, bid_amount)

            if tx_hash:
                bt.logging.success(
                    f"Catch-up bid placed on auction {auction.auction_id}: "
                    f"amount={bid_amount}, tx={tx_hash}"
                )
