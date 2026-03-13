"""
Validator event listener for TensorUSD liquidation auctions.

Uses the shared AuctionEventListener and stores events in SQLite.
"""

from typing import Optional

from scalecodec.base import ScaleBytes
from substrateinterface import SubstrateInterface
from substrateinterface.contracts import ContractEvent, ContractMetadata
from sqlalchemy.orm import Session

import bittensor as bt

from tensorusd.auction.event_listener import AuctionEventListener
from tensorusd.auction.contract import TensorUSDAuctionContract
from tensorusd.auction.types import (
    AuctionEvent,
    AuctionEventType,
    AuctionFinalizedEvent,
)
from tensorusd.validator.db.models import AuctionWin, SessionFactory


class ValidatorEventListener:
    """
    Wrapper around AuctionEventListener that stores events in SQLite.
    """

    def __init__(
        self,
        substrate: SubstrateInterface,
        contract_address: str,
        metadata_path: str,
        db_session_factory: SessionFactory,
        auction_contract: Optional[TensorUSDAuctionContract] = None,
    ):
        """
        Initialize the validator event listener.

        Args:
            substrate: Shared SubstrateInterface instance
            contract_address: SS58 address of the auction contract
            metadata_path: Path to tusdt_auction.json metadata file
            db_session_factory: SQLAlchemy session factory for DB access
            auction_contract: Optional auction contract for fetching auction data
        """
        self.substrate = substrate
        self.contract_address = contract_address
        self.metadata_path = metadata_path
        self.db_session_factory = db_session_factory
        self.auction_contract = auction_contract
        self.contract_metadata: Optional[ContractMetadata] = None

        # Use shared event listener with our callback
        self._listener = AuctionEventListener(
            substrate=substrate,
            contract_address=contract_address,
            metadata_path=metadata_path,
            callback=self._handle_event,
        )

    def _handle_event(self, event: AuctionEvent):
        """Store AuctionFinalized events in SQLite database."""
        if event.event_type != AuctionEventType.FINALIZED:
            return

        session = self.db_session_factory()
        try:
            self._store_win(session, event)
            session.commit()
        except Exception as e:
            bt.logging.error(f"Error storing event: {e}")
            session.rollback()
        finally:
            session.close()

    def _store_win(self, session: Session, event: AuctionFinalizedEvent):
        """Store auction win in database."""
        if not event.winner:
            return

        existing = (
            session.query(AuctionWin)
            .filter(AuctionWin.auction_id == event.auction_id)
            .first()
        )
        if not existing:
            win = AuctionWin(
                auction_id=event.auction_id,
                winner_hotkey=event.highest_bid_metadata.get("hot_key"),
                winning_bid=event.highest_bid,
                debt_balance=event.debt_balance,
                block_number=event.block_number,
            )
            session.add(win)
            bt.logging.info(
                f"Recorded auction win: auction_id={event.auction_id}, "
                f"winner coldkey={event.winner}, winner hotkey={event.highest_bid_metadata.get('hot_key')}, winning bid={event.highest_bid}, debt={event.debt_balance}"
            )

    def sync_historical_wins(self, start_block: int, end_block: int):
        """
        Sync historical AuctionFinalized events from a block range.

        Should be called once at startup before starting the live listener.

        Args:
            start_block: Starting block number (inclusive)
            end_block: Ending block number (inclusive)
        """
        bt.logging.info(
            f"Syncing historical wins from block {start_block} to {end_block}"
        )

        # Load contract metadata if not already loaded
        try:
            if self.contract_metadata is None:
                self.contract_metadata = ContractMetadata.create_from_file(
                    metadata_file=self.metadata_path,
                    substrate=self.substrate,
                )
        except Exception as e:
            bt.logging.error(f"Error loading contract metadata: {e}")
            return

        wins_found = 0
        session = self.db_session_factory()

        try:
            for block_num in range(start_block, end_block + 1):
                if block_num % 100 == 0:
                    bt.logging.debug(f"Scanning block {block_num}...")

                try:
                    block_hash = self.substrate.get_block_hash(block_num)
                    if not block_hash:
                        continue

                    events = self.substrate.get_events(block_hash)

                    for event in events:
                        if self._is_contract_event(event):
                            auction_event = self._decode_finalized_event(
                                event, block_num
                            )
                            if auction_event:
                                self._store_win(session, auction_event)
                                wins_found += 1

                except Exception as e:
                    bt.logging.warning(f"Error scanning block {block_num}: {e}")
                    continue

            session.commit()
            bt.logging.success(
                f"Historical sync complete: {wins_found} wins found "
                f"(blocks {start_block}-{end_block})"
            )

        except Exception as e:
            bt.logging.error(f"Error during historical sync: {e}")
            session.rollback()
        finally:
            session.close()

    def _is_contract_event(self, event) -> bool:
        """Check if event is from our auction contract."""
        try:
            return (
                event.value["event"]["module_id"] == "Contracts"
                and event.value["event"]["event_id"] == "ContractEmitted"
                and event.value["event"]["attributes"]["contract"]
                == self.contract_address
            )
        except (KeyError, TypeError):
            return False

    def _decode_finalized_event(
        self, event, block_number: int
    ) -> Optional[AuctionEvent]:
        """Decode contract event, returning only AuctionFinalized events."""
        try:
            contract_data = event["event"][1][1]["data"].value_object

            if self.contract_metadata.metadata_version >= 5:
                for topic in event.value["topics"]:
                    event_id = self.contract_metadata.get_event_id_by_topic(topic)
                    if event_id is not None:
                        event_bytes = (
                            self.substrate.create_scale_object("U8")
                            .encode(event_id)
                            .data
                        )
                        contract_data = event_bytes + contract_data

            contract_event_obj = ContractEvent(
                data=ScaleBytes(contract_data),
                runtime_config=self.substrate.runtime_config,
                contract_metadata=self.contract_metadata,
            )
            contract_event_obj.decode()

            value_object = contract_event_obj.value_object
            print(value_object)
            event_name = value_object.get("name")

            # Only process AuctionFinalized events
            if event_name != "AuctionFinalized":
                return None

            args = value_object.get("args", [])
            event_data = {arg["label"]: arg["value"] for arg in args}

            return AuctionFinalizedEvent(
                event_type=AuctionEventType.FINALIZED,
                block_number=block_number,
                auction_id=event_data.get("auction_id"),
                winner=event_data.get("winner"),
                highest_bid=event_data.get("highest_bid"),
                debt_balance=event_data.get("debt_balance"),
                highest_bid_metadata=event_data.get("highest_bid_metadata"),
            )

        except Exception as e:
            bt.logging.warning(f"Failed to decode contract event: {e}")
            return None

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
