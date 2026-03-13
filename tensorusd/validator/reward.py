# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2024 TensorUSD

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import numpy as np
from typing import List, Dict, Tuple, Optional
import bittensor as bt

from tensorusd.validator.db import SessionFactory
from tensorusd.validator.db.models import AuctionWin


# Reward calculation constants
BASE_REWARD = 1.0  # Base reward for paying exactly debt amount
BONUS_THRESHOLD = 0.20  # 20% overpay for max bonus


def calculate_win_reward(winning_bid: int, debt_balance: int) -> float:
    """
    Calculate reward for a single auction win based on bid vs debt.

    Linear reward scaling:
    - Paying exactly debt = BASE_REWARD (1.0)
    - Paying 20%+ more = BASE_REWARD + MAX_BONUS_MULTIPLIER (2.0)
    - Linear interpolation between

    Args:
        winning_bid: Amount paid by winner
        debt_balance: Debt amount of the auction
        contract_params: Contract parameters
    Returns:
        Reward value for this win
    """

    bonus_ratio = min((winning_bid - debt_balance) / debt_balance, BONUS_THRESHOLD)
    reward = bonus_ratio + BASE_REWARD
    return reward


def get_auction_rewards_from_db(
    db_session_factory: SessionFactory,
    metagraph: bt.Metagraph,
    tempo_start_block: int,
    tempo_end_block: int,
    burn_uid: Optional[int] = None,
    burn_weight_percent: float = 0.0,
) -> Tuple[np.ndarray, List[int]]:
    """
    Calculate rewards from SQLite for auctions finalized within tempo window.

    Linear reward calculation based on bid amount relative to debt:
    - Base reward for paying debt amount
    - Bonus reward (up to 2x) for paying 20%+ more than debt
    - Multiple wins accumulate rewards

    Args:
        db_session_factory: SQLAlchemy session factory
        metagraph: Bittensor metagraph for hotkey lookup
        tempo_start_block: Start of current tempo (exclusive)
        tempo_end_block: End of current tempo (inclusive)
        burn_uid: Optional UID to give burn weight
        burn_weight_percent: Percentage of total weight to reserve (0.0-1.0)

    Returns:
        Tuple of (rewards array, list of miner UIDs)
    """
    session = db_session_factory()
    try:
        # Get wins from past tempo blocks only (not yet processed)
        wins = (
            session.query(AuctionWin)
            .filter(
                AuctionWin.block_number > tempo_start_block,
                AuctionWin.block_number <= tempo_end_block,
                AuctionWin.tempo_block.is_(None),  # Not yet processed
            )
            .all()
        )
        # Calculate rewards per hotkey (accumulate for multiple wins)
        hotkey_rewards: Dict[str, float] = {}
        processed_wins = []

        for win in wins:
            # Check if winner is a registered miner
            if win.winner_hotkey in metagraph.hotkeys:
                reward = calculate_win_reward(win.winning_bid, win.debt_balance)
                hotkey_rewards[win.winner_hotkey] = (
                    hotkey_rewards.get(win.winner_hotkey, 0.0) + reward
                )
                processed_wins.append(win)
        # Mark wins as processed
        for win in processed_wins:
            win.tempo_block = tempo_end_block

        session.commit()

        # Build rewards array for all UIDs
        rewards = []
        uids = []

        for uid in range(metagraph.n.item()):
            hotkey = metagraph.hotkeys[uid]
            if hotkey in hotkey_rewards:
                reward_val = hotkey_rewards[hotkey]
                rewards.append(reward_val)
                uids.append(uid)

        # Apply burn weight if specified
        total_reward = sum(rewards)
        if burn_uid is not None and 0 < burn_weight_percent < 1.0:
            if total_reward > 0:
                burn_weight = (
                    total_reward / (1 - burn_weight_percent)
                ) * burn_weight_percent
                rewards.append(burn_weight)
                uids.append(burn_uid)
                bt.logging.info(
                    f"Burning {burn_weight_percent * 100:.1f}% of total reward to UID {burn_uid}"
                )
            else:
                bt.logging.info(f"No auction wins - giving UID {burn_uid} weight of 1")
                rewards = [1.0]
                uids = [burn_uid]
        elif burn_weight_percent == 1:
            uids = [burn_uid]
            rewards = [1]

        # If no rewards were given, give burn_uid or UID 0 a weight of 1
        if sum(rewards) == 0:
            bt.logging.info(f"No auction wins - giving UID {burn_uid} weight of 1")
            rewards = [1.0]
            uids = [burn_uid]

        bt.logging.info(
            f"Auction rewards calculated: {len(processed_wins)} wins, "
            f"{len(hotkey_rewards)} unique winners, total_reward={sum(rewards):.4f}"
        )

        return np.array(rewards), uids

    except Exception as e:
        bt.logging.error(f"Error calculating auction rewards: {e}")
        session.rollback()
        return [1.0], [burn_uid]

    finally:
        session.close()
