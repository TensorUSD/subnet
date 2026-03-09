# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# TODO(developer): Set your name
# Copyright © 2023 <your name>

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
import numpy as np
from typing import List, Dict, Tuple, Callable
import bittensor as bt

from tensorusd.validator.db.models import AuctionWin


def reward(query: int, response: int) -> float:
    """
    Reward the miner response to the dummy request. This method returns a reward
    value for the miner, which is used to update the miner's score.

    Returns:
    - float: The reward value for the miner.
    """
    bt.logging.info(
        f"In rewards, query val: {query}, response val: {response}, rewards val: {1.0 if response == query * 2 else 0}"
    )
    return 1.0 if response == query * 2 else 0


def get_rewards(
    self,
    query: int,
    responses: List[float],
) -> np.ndarray:
    """
    Returns an array of rewards for the given query and responses.

    Args:
    - query (int): The query sent to the miner.
    - responses (List[float]): A list of responses from the miner.

    Returns:
    - np.ndarray: An array of rewards for the given query and responses.
    """
    # Get all the reward results by iteratively calling your reward() function.

    return np.array([reward(query, response) for response in responses])


def get_auction_rewards_from_db(
    db_session_factory: Callable,
    metagraph: bt.Metagraph,
    tempo_start_block: int,
    tempo_end_block: int,
) -> Tuple[np.ndarray, List[int]]:
    """
    Calculate rewards from SQLite for auctions finalized within tempo window.

    Args:
        db_session_factory: SQLAlchemy session factory
        metagraph: Bittensor metagraph for hotkey lookup
        tempo_start_block: Start of current tempo (exclusive)
        tempo_end_block: End of current tempo (inclusive)

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

        # Build hotkey to UID mapping
        hotkey_to_uid = {
            hotkey: uid
            for uid, hotkey in enumerate(metagraph.hotkeys)
        }

        # Count wins per hotkey
        win_counts: Dict[str, int] = {}
        processed_wins = []

        for win in wins:
            # Check if winner is a registered miner
            if win.winner_hotkey in hotkey_to_uid:
                win_counts[win.winner_hotkey] = (
                    win_counts.get(win.winner_hotkey, 0) + 1
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
            reward_val = float(win_counts.get(hotkey, 0))
            rewards.append(reward_val)
            uids.append(uid)

        # If no rewards were given, give UID 0 (owner) a weight of 1
        if sum(rewards) == 0:
            bt.logging.info("No auction wins - giving UID 0 (owner) weight of 1")
            if len(rewards) > 0:
                rewards[0] = 1.0

        bt.logging.info(
            f"Auction rewards calculated: {len(processed_wins)} wins, "
            f"{len(win_counts)} unique winners"
        )

        return np.array(rewards), uids

    except Exception as e:
        bt.logging.error(f"Error calculating auction rewards: {e}")
        session.rollback()
        # On error, also give UID 0 weight of 1 as fallback
        return np.array([1.0]), [0]

    finally:
        session.close()
