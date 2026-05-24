from dataclasses import dataclass
from typing import Optional


@dataclass
class MinerBidConfig:
    """
    Configuration for miner bidding strategy.

    Attributes:
        initial_bid_percentage: Percentage of collateral value for first bid (0.0-1.0)
        bid_increment_rate: Percentage to increase when outbid (0.0-1.0)
        max_bid_percentage: Maximum bid as percentage of collateral (0.0-1.0)
        max_bid_absolute: Absolute maximum bid in token units (optional)
        min_profit_margin: Minimum profit margin required to bid (0.0-1.0)
    """

    initial_bid_percentage: float = 0.05
    bid_increment_rate: float = 0.05
    max_bid_percentage: float = 0.95
    max_bid_absolute: Optional[int] = None
    min_profit_margin: float = 0.02

    def __post_init__(self):
        if not 0 < self.initial_bid_percentage <= 1:
            raise ValueError("initial_bid_percentage must be between 0 and 1")
        if not 0 < self.bid_increment_rate <= 1:
            raise ValueError("bid_increment_rate must be between 0 and 1")
        if not 0 < self.max_bid_percentage <= 1:
            raise ValueError("max_bid_percentage must be between 0 and 1")
        if not 0 <= self.min_profit_margin < 1:
            raise ValueError("min_profit_margin must be between 0 and 1")
        if self.max_bid_absolute is not None and self.max_bid_absolute <= 0:
            raise ValueError("max_bid_absolute must be positive")
