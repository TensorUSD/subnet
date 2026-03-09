"""
Bidding strategy for TensorUSD liquidation auctions.

Profit calculation:
- collateral_value = collateral_amount * collateral_price (in USDT)
- debt is already in USDT (stable coin)
- profit = collateral_value - bid_amount - debt
"""

import bittensor as bt

from tensorusd.auction.config import MinerBidConfig


class BiddingStrategy:
    """
    Calculates optimal bid amounts based on configured strategy.

    Strategy parameters:
    - initial_bid_percentage: Starting bid as % of collateral value
    - bid_increment_rate: How much to increase when outbid
    - max_bid_percentage: Maximum bid as % of collateral value
    - min_profit_margin: Minimum profit required to bid
    """

    def __init__(self, config: MinerBidConfig):
        """
        Initialize bidding strategy.

        Args:
            config: Bidding configuration parameters
        """
        self.config = config

    def calculate_collateral_value(
        self,
        collateral: int,
        collateral_price: int,
    ) -> int:
        """
        Calculate collateral value in USDT terms.

        Args:
            collateral: Collateral amount in token units
            collateral_price: Price per collateral token (scaled by PRICE_DECIMALS)

        Returns:
            Collateral value in USDT
        """
        return collateral * collateral_price

    def calculate_bid(
        self,
        collateral: int,
        debt: int,
        current_bid: int,
        collateral_price: int,
    ) -> int:
        """
        Calculate optimal bid amount in USDT.

        Args:
            collateral: Total collateral in vault (token units)
            debt: Total debt in vault (USDT)
            current_bid: Current highest bid in USDT (0 if first bid)
            collateral_price: Price per collateral token (scaled by PRICE_DECIMALS)

        Returns:
            Calculated bid amount in USDT, or 0 if should not bid
        """
        # Calculate collateral value in USDT
        collateral_value = self.calculate_collateral_value(collateral, collateral_price)

        # Calculate potential profit (what we'd get if we win with debt payment)
        max_profitable_bid = collateral_value - debt

        # If no profit possible, don't bid
        if max_profitable_bid <= 0:
            bt.logging.info(
                f"No profit possible: collateral_value={collateral_value}, debt={debt}"
            )
            return 0

        # Calculate bid based on strategy
        if current_bid == 0:
            # First bid - use initial percentage of collateral value
            bid = int(debt * (1 + self.config.initial_bid_percentage))
        else:
            # Outbid by increment
            bid = int(current_bid * (1 + self.config.bid_increment_rate))

        # Apply maximum limits (as % of collateral value)
        max_bid = int(debt * (1 + self.config.max_bid_percentage))
        if self.config.max_bid_absolute is not None:
            max_bid = min(max_bid, self.config.max_bid_absolute)

        bid = min(bid, max_bid)

        # Ensure minimum profit margin
        expected_profit = collateral_value - bid
        min_required_profit = int(collateral_value * self.config.min_profit_margin)

        if expected_profit < min_required_profit:
            bt.logging.info(
                f"Bid {bid} doesn't meet profit margin: "
                f"expected_profit={expected_profit}, "
                f"min_required={min_required_profit}"
            )
            return 0

        bt.logging.info(
            f"Calculated bid: {bid} TUSDT "
            f"(collateral_value={collateral_value}, debt={debt}, "
            f"expected_profit={expected_profit})"
        )

        return bid

    def should_bid(
        self,
        collateral: int,
        debt: int,
        current_bid: int,
        collateral_price: int,
        auction_end_time: int,
        current_time: int,
    ) -> bool:
        """
        Determine if miner should participate in this auction.

        Args:
            collateral: Total collateral in vault (token units)
            debt: Total debt in vault (USDT)
            current_bid: Current highest bid (USDT)
            collateral_price: Price per collateral token
            auction_end_time: Timestamp when auction ends (ms)
            current_time: Current timestamp (ms)

        Returns:
            True if should bid, False otherwise
        """
        # Check auction hasn't expired
        if auction_end_time <= current_time:
            bt.logging.debug("Auction has expired")
            return False

        # Check profitability
        bid = self.calculate_bid(collateral, debt, current_bid, collateral_price)
        return bid > 0

    def should_rebid(self, my_last_bid: int, current_highest: int) -> bool:
        return current_highest > my_last_bid
