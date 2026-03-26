"""
Reusable event listener for TensorUSD liquidation auctions.

Can be used by both miners and validators with custom callbacks.
"""

import threading
from typing import Callable, Optional, Set

from scalecodec.base import ScaleBytes
from substrateinterface import SubstrateInterface
from substrateinterface.contracts import ContractEvent, ContractMetadata

import bittensor as bt

from tensorusd.auction import AuctionCreatedEvent, AuctionFinalizedEvent, BidPlacedEvent
from tensorusd.auction.types import AuctionEventType, AuctionUnionEvent


class AuctionEventListener:
    """
    Background thread that listens for auction contract events.

    Uses a callback pattern so both miners and validators can
    handle events in their own way.
    """

    def __init__(
        self,
        substrate: SubstrateInterface,
        contract_address: str,
        metadata_path: str,
        callback: Callable[[AuctionUnionEvent], None],
    ):
        """
        Initialize the event listener.

        Args:
            substrate: Shared SubstrateInterface instance
            contract_address: SS58 address of the auction contract
            metadata_path: Path to tusdt_auction.json metadata file
            callback: Function to call when auction event is detected
        """
        self.substrate = substrate
        self.contract_address = contract_address
        self.metadata_path = metadata_path
        self.callback = callback

        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: Optional[threading.Thread] = None

        self.contract_metadata: Optional[ContractMetadata] = None

    def _ensure_metadata_loaded(self):
        """Load contract metadata once for decoding events."""
        if self.contract_metadata is None:
            self.contract_metadata = ContractMetadata.create_from_file(
                metadata_file=self.metadata_path,
                substrate=self.substrate,
            )

    def run(self):
        """Main loop - subscribe to events (runs in background thread)."""
        self._ensure_metadata_loaded()

        bt.logging.info(
            f"Event listener started for Auction contract {self.contract_address}"
        )

        try:
            self.substrate.subscribe_block_headers(self._subscription_handler)
        except Exception as e:
            bt.logging.error(f"Event listener error: {e}")

    def sync_historical_events(
        self,
        start_block: int,
        end_block: int,
        event_types: Optional[Set[AuctionEventType]] = None,
        progress_log_interval: int = 200,
    ) -> int:
        """
        Process historical auction events in a bounded block range.

        Args:
            start_block: Inclusive start block.
            end_block: Inclusive end block.
            event_types: Optional filter for specific auction event types.
            progress_log_interval: How often to log progress while scanning.

        Returns:
            Number of decoded events passed to callback.
        """
        if end_block < start_block:
            return 0

        self._ensure_metadata_loaded()
        decoded_count = 0

        for block_number in range(start_block, end_block + 1):
            try:
                block_hash = self.substrate.get_block_hash(block_number)
                events = self.substrate.get_events(block_hash)
            except Exception as e:
                bt.logging.warning(
                    f"Skipping block {block_number} during historical sync: {e}"
                )
                continue

            for event in events:
                if not self._is_contract_event(event):
                    continue

                decoded_event = self._decode_contract_event(event, block_number)
                if decoded_event is None:
                    continue
                if event_types and decoded_event.event_type not in event_types:
                    continue

                decoded_count += 1
                try:
                    self.callback(decoded_event)
                except Exception as e:
                    bt.logging.error(f"Error in historical event callback: {e}")

            if progress_log_interval > 0 and block_number % progress_log_interval == 0:
                bt.logging.info(
                    f"Historical auction scan progress: block={block_number}, "
                    f"decoded_events={decoded_count}"
                )

        return decoded_count

    def _subscription_handler(self, obj, update_nr, subscription_id):
        """Handle new blocks."""
        if self.should_exit:
            return

        block_number = obj["header"]["number"]
        block_hash = self.substrate.get_block_hash(block_number)
        events = self.substrate.get_events(block_hash)

        for event in events:
            if self._is_contract_event(event):
                decoded_event = self._decode_contract_event(event, block_number)
                if decoded_event:
                    try:
                        self.callback(decoded_event)
                    except Exception as e:
                        bt.logging.error(f"Error in event callback: {e}")

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

    def _decode_contract_event(
        self, event, block_number: int
    ) -> Optional[AuctionUnionEvent]:
        """Decode contract event data."""
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

            return self._to_auction_event(contract_event_obj, block_number)

        except Exception as e:
            bt.logging.warning(f"Failed to decode contract event: {e}")
            return None

    def _parse_event_args(self, value_object: dict) -> dict:
        """
        Parse event args from value_object format to a simple dict.
        """
        args = value_object.get("args", [])
        return {arg["label"]: arg["value"] for arg in args}

    def _to_auction_event(
        self, contract_event, block_number: int
    ) -> Optional[AuctionUnionEvent]:
        """Convert decoded contract event to AuctionUnionEvent."""
        value_object = contract_event.value_object
        event_name = value_object.get("name")
        event_data = self._parse_event_args(value_object)

        if event_name == "AuctionCreated":
            return AuctionCreatedEvent(
                event_type=AuctionEventType.CREATED,
                block_number=block_number,
                auction_id=event_data.get("auction_id"),
                vault_owner=event_data.get("vault_owner"),
                vault_id=event_data.get("vault_id"),
                starts_at=event_data.get("starts_at"),
                ends_at=event_data.get("ends_at"),
            )

        elif event_name == "BidPlaced":
            return BidPlacedEvent(
                event_type=AuctionEventType.BID_PLACED,
                block_number=block_number,
                auction_id=event_data.get("auction_id"),
                bid_id=event_data.get("bid_id"),
                bidder=event_data.get("bidder"),
                amount=event_data.get("amount"),
            )

        elif event_name == "AuctionFinalized":
            return AuctionFinalizedEvent(
                event_type=AuctionEventType.FINALIZED,
                block_number=block_number,
                auction_id=event_data.get("auction_id"),
                winner=event_data.get("winner"),
                highest_bid=event_data.get("highest_bid"),
                debt_balance=event_data.get("debt_balance"),
                highest_bid_metadata=event_data.get("highest_bid_metadata"),
            )

        return None

    def run_in_background_thread(self):
        """Start listener in background thread."""
        if not self.is_running:
            bt.logging.debug("Starting event listener in background thread.")
            self.should_exit = False
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True

    def stop_run_thread(self):
        """Stop the background thread."""
        if self.is_running:
            bt.logging.debug("Stopping event listener.")
            self.should_exit = True
            if self.thread is not None:
                self.thread.join(5)
            self.is_running = False
