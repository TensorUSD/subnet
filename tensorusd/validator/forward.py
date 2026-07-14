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

import asyncio
import numpy as np
import bittensor as bt

from neurons.validator import Validator
from tensorusd.auth.config import settings
from tensorusd.utils.subnet import get_dynamic_info, get_synchroized_sleep_time
from tensorusd.validator.reward import get_auction_rewards_from_db

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neurons.validator import Validator


async def forward(self: "Validator"):
    dynamic_info = get_dynamic_info(self.subtensor, self.config.netuid)
    if self.is_first_run:
        self.is_first_run = False
        sleep_time = get_synchroized_sleep_time(
            dynamic_info["last_step_block"], self.block, dynamic_info["tempo"]
        )
    else:
        sleep_time = (self.tempo // 2) * 12
    rewards, uids = get_auction_rewards_from_db(
        db_session_factory=self.db_session_factory,
        metagraph=self.metagraph,
        tempo_start_block=dynamic_info["last_step_block"],
        tempo_end_block=self.block,
        burn_uid=0,
        # TODO: get this from api
        burn_weight_percent=0,
    )

    # Agent bonus: if enabled, add 20% of total auction rewards to the best agent winner
    # Only applies if the winner also participates in liquidation (already in uids list)
    if settings.agent_bonus_enabled:
        try:
            best_meta = self.client.get_best_submission_meta()
            if best_meta:
                winner_hotkey = best_meta.get("miner_hotkey")
                if winner_hotkey and winner_hotkey in self.metagraph.hotkeys:
                    winner_uid = self.metagraph.hotkeys.index(winner_hotkey)
                    if winner_uid in uids:
                        idx = uids.index(winner_uid)
                        winner_reward = float(rewards[idx])
                        bonus = winner_reward * settings.agent_bonus_percent
                        rewards[idx] += bonus
                        bt.logging.info(
                            "Agent bonus (%.4f = %.1f%% of %.4f) added to UID %d (%s)",
                            bonus,
                            settings.agent_bonus_percent * 100,
                            winner_reward,
                            winner_uid,
                            winner_hotkey,
                        )
        except Exception as exc:
            bt.logging.warning("Failed to fetch best agent for bonus: %s", exc)

    bt.logging.info(f"Rewards: {rewards}, Uids: {uids}, Mechid: 0")
    self.update_scores(rewards, uids, 0)
    await asyncio.sleep(sleep_time)
