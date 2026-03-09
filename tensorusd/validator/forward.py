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

import time
import bittensor as bt

from tensorusd.protocol import Dummy
from tensorusd.validator.reward import get_rewards, get_auction_rewards_from_db
from tensorusd.utils.uids import get_random_uids
from tensorusd.utils.misc import ttl_get_block


# Tempo length in blocks (360 blocks = ~72 minutes at 12s/block)
TEMPO_BLOCKS = 360


async def forward(self):
    """
    The forward function is called by the validator every time step.

    It is responsible for querying the network and scoring the responses.

    Additionally, at the end of each tempo (360 blocks), it calculates
    rewards for auction wins from the SQLite database.

    Args:
        self (:obj:`bittensor.neuron.Neuron`): The neuron object which contains all the necessary state for the validator.

    """
    # Check if we should calculate auction rewards (at end of tempo)
    if hasattr(self, "auction_enabled") and self.auction_enabled:
        await _check_tempo_rewards(self)

    # TODO(developer): Define how the validator selects a miner to query, how often, etc.
    # get_random_uids is an example method, but you can replace it with your own.
    miner_uids = get_random_uids(self, k=self.config.neuron.sample_size)

    # The dendrite client queries the network.
    responses = await self.dendrite(
        # Send the query to selected miner axons in the network.
        axons=[self.metagraph.axons[uid] for uid in miner_uids],
        # Construct a dummy query. This simply contains a single integer.
        synapse=Dummy(dummy_input=self.step),
        # All responses have the deserialize function called on them before returning.
        # You are encouraged to define your own deserialization function.
        deserialize=True,
    )

    # Log the results for monitoring purposes.
    bt.logging.info(f"Received responses: {responses}")

    # TODO(developer): Define how the validator scores responses.
    # Adjust the scores based on responses from miners.
    rewards = get_rewards(self, query=self.step, responses=responses)

    bt.logging.info(f"Scored responses: {rewards}")
    # Update the scores based on the rewards. You may want to define your own update_scores function for custom behavior.
    self.update_scores(rewards, miner_uids)
    time.sleep(5)


async def _check_tempo_rewards(self):
    """
    Check if tempo has ended and calculate auction rewards.

    At the end of each tempo (360 blocks), fetch auction wins from SQLite
    and update miner scores accordingly.
    """
    current_block = ttl_get_block(self)

    # TODO: User will implement tempo end detection manually
    # For now, check if 360 blocks have passed since last tempo
    if not _should_set_weights(self, current_block):
        return

    bt.logging.info("Tempo ended - calculating auction rewards from DB...")

    tempo_start = self.last_tempo_block
    tempo_end = current_block

    # Verify we only process events from past TEMPO_BLOCKS
    if tempo_end - tempo_start > TEMPO_BLOCKS:
        bt.logging.warning(
            f"Tempo window too large: {tempo_end - tempo_start} > {TEMPO_BLOCKS}. "
            f"Adjusting start to {tempo_end - TEMPO_BLOCKS}"
        )
        tempo_start = tempo_end - TEMPO_BLOCKS

    # Calculate rewards from SQLite
    rewards, uids = get_auction_rewards_from_db(
        self.db_session_factory,
        self.metagraph,
        tempo_start,
        tempo_end,
    )

    if len(rewards) > 0 and sum(rewards) > 0:
        self.update_scores(rewards, uids)
        bt.logging.info(f"Updated auction scores: {sum(rewards)} total rewards")

    # Update last tempo block
    self.last_tempo_block = tempo_end


def _should_set_weights(self, current_block: int) -> bool:
    """
    Check if we should set weights (tempo has ended).

    TODO: User will implement this logic manually.
    Current implementation: check if TEMPO_BLOCKS have passed.
    """
    if not hasattr(self, "last_tempo_block"):
        self.last_tempo_block = current_block
        return False

    return current_block - self.last_tempo_block >= TEMPO_BLOCKS
