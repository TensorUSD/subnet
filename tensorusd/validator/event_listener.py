"""
Validator event listener for TensorUSD liquidation auctions.

Uses the shared AuctionEventListener and stores events in SQLite.
"""

from typing import Callable

from substrateinterface import SubstrateInterface

import bittensor as bt

from tensorusd.auction.event_listener import AuctionEventListener
from tensorusd.auction.types import AuctionEvent, AuctionEventType
from tensorusd.validator.db.models import AuctionEventModel, AuctionWin


class ValidatorEventListener:
    """
    Wrapper around AuctionEventListener that stores events in SQLite.
    """

    def __init__(
        self,
        substrate: SubstrateInterface,
        contract_address: str,
        metadata_path: str,
        db_session_factory: Callable,
    ):
        """
        Initialize the validator event listener.

        Args:
            substrate: Shared SubstrateInterface instance
            contract_address: SS58 address of the auction contract
            metadata_path: Path to tusdt_auction.json metadata file
            db_session_factory: SQLAlchemy session factory for DB access
        """
        self.db_session_factory = db_session_factory

        # Use shared event listener with our callback
        self._listener = AuctionEventListener(
            substrate=substrate,
            contract_address=contract_address,
            metadata_path=metadata_path,
            callback=self._handle_event,
        )

    def _handle_event(self, event: AuctionEvent):
        """Store event in SQLite database."""
        session = self.db_session_factory()
        try:
            self._store_event(session, event)
            session.commit()
        except Exception as e:
            bt.logging.error(f"Error storing event: {e}")
            session.rollback()
        finally:
            session.close()

    def _store_event(self, session, event: AuctionEvent):
        """Store event in SQLite database."""
        # Create event record
        db_event = AuctionEventModel(
            event_type=event.event_type.value,
            auction_id=event.auction_id,
            block_number=event.block_number,
            vault_owner=event.vault_owner,
            vault_id=event.vault_id,
            bidder=event.bidder,
            bid_id=event.bid_id,
            bid_amount=event.amount,
            winner=event.winner,
            winning_bid=event.highest_bid,
        )
        session.add(db_event)

        # If auction finalized, also add to wins table
        if event.event_type == AuctionEventType.FINALIZED and event.winner:
            # Check if win already exists (avoid duplicates)
            existing = (
                session.query(AuctionWin)
                .filter(AuctionWin.auction_id == event.auction_id)
                .first()
            )
            if not existing:
                win = AuctionWin(
                    auction_id=event.auction_id,
                    winner_hotkey=event.winner,
                    winning_bid=event.highest_bid,
                    block_number=event.block_number,
                )
                session.add(win)

                bt.logging.info(
                    f"Recorded auction win: auction_id={event.auction_id}, "
                    f"winner={event.winner}, bid={event.highest_bid}"
                )

    def run_in_background_thread(self):
        """Start listener in background thread."""
        self._listener.run_in_background_thread()

    def stop_run_thread(self):
        """Stop the background thread."""
        self._listener.stop_run_thread()

    @property
    def is_running(self) -> bool:
        """Check if listener is running."""
        return self._listener.is_running
