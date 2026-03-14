from dataclasses import dataclass
from typing import Optional
from enum import Enum
import typing


class AuctionEventType(Enum):
    """Types of auction events from the TensorUSD auction contract."""

    CREATED = "AuctionCreated"
    BID_PLACED = "BidPlaced"
    FINALIZED = "AuctionFinalized"


@dataclass
class BaseAuctionEvent:
    """
    Base class for all auction events.

    Fields vary based on event_type:
    - CREATED: auction_id, vault_owner, vault_id, starts_at, ends_at
    - BID_PLACED: auction_id, bid_id, bidder, amount
    - FINALIZED: auction_id, winner, highest_bid
    """

    event_type: AuctionEventType
    block_number: int
    auction_id: int


@dataclass
class AuctionCreatedEvent(BaseAuctionEvent):
    """
    Event for when a new auction is created.
    """

    vault_owner: str
    vault_id: int
    starts_at: int
    ends_at: int


@dataclass
class BidPlacedEvent(BaseAuctionEvent):
    """
    Event for when a bid is placed on an auction.
    """

    bid_id: int
    bidder: str
    amount: int


@dataclass
class AuctionFinalizedEvent(BaseAuctionEvent):
    winner: str
    highest_bid: int
    debt_balance: int
    highest_bid_metadata: Optional[dict] = None


@dataclass
class AuctionResult:
    """
    Final result of a liquidation auction.
    Used for reward calculation.
    """

    auction_id: int
    winner: str  # AccountId (SS58 address)
    winning_bid: int
    vault_owner: str
    vault_id: int
    finalized_at_block: int


AuctionUnionEvent = typing.Union[
    AuctionCreatedEvent, BidPlacedEvent, AuctionFinalizedEvent
]
