from dataclasses import dataclass
from typing import Optional
from enum import Enum


class AuctionEventType(Enum):
    """Types of auction events from the TensorUSD auction contract."""

    CREATED = "AuctionCreated"
    BID_PLACED = "BidPlaced"
    FINALIZED = "AuctionFinalized"


@dataclass
class AuctionEvent:
    """
    Represents a decoded auction event from the blockchain.

    Fields vary based on event_type:
    - CREATED: auction_id, vault_owner, vault_id, starts_at, ends_at
    - BID_PLACED: auction_id, bid_id, bidder, amount
    - FINALIZED: auction_id, winner, highest_bid
    """

    event_type: AuctionEventType
    block_number: int
    auction_id: int
    vault_owner: Optional[str] = None
    vault_id: Optional[int] = None
    bidder: Optional[str] = None
    bid_id: Optional[int] = None
    amount: Optional[int] = None
    winner: Optional[str] = None
    highest_bid: Optional[int] = None
    starts_at: Optional[int] = None
    ends_at: Optional[int] = None


@dataclass
class AuctionFinalizedEvent(AuctionEvent):
    debt_balance: Optional[int] = None
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
