import asyncio
import traceback
import numpy as np
import bittensor as bt
from typing import TYPE_CHECKING

from tensorusd.auth.auth import _log_event
from tensorusd.validator.reward import calculate_rewards_for_mech1
from tensorusd.base.utils.weight_utils import convert_weights_and_uids_for_emit

if TYPE_CHECKING:
    from neurons.validator import AgentValidator

SLEEP_TIME = 1500
MAX_RETRIES = 3


async def forward_mech1(self: "AgentValidator"):
    try:
        n_uids = int(self.metagraph.n)
        all_uids = self.metagraph.uids
        all_weights = [0] * n_uids

        rewards = calculate_rewards_for_mech1(
            self.subtensor, self.config.netuid, self.agent_vali._client
        )

        for uid, reward in rewards:
            all_weights[uid] = reward

        _log_event(f"\n Rewards: \n{all_weights} \n UIDs: \n{all_uids} \n MECH_ID=1")

        uids, weights = convert_weights_and_uids_for_emit(
            np.array(all_uids), np.array(all_weights)
        )

        self.update_scores(weights, uids, 1)

        set_weights_success = False

        for attempt in range(MAX_RETRIES):
            try:
                result, msg = self.subtensor.set_weights(
                    wallet=self.wallet,
                    netuid=self.config.netuid,
                    uids=uids,
                    weights=weights,
                    mechid=self.config.mechid,
                    version_key=self.spec_version,
                    wait_for_finalization=False,
                    wait_for_inclusion=False,
                )

                if result is True:
                    _log_event("set_weights on chain successfully!")
                    set_weights_success = True
                    break
                else:
                    _log_event(f"set_weights attempt {attempt + 1} failed: {msg}")
            except Exception as w_err:
                _log_event("set_weights failed", w_err)
                await asyncio.sleep(5)
        if not set_weights_success:
            _log_event("Failed to set weights after maximum retries.")

    except Exception as e:
        _log_event(f"Error in forward pass: {e}")
        _log_event(traceback.format_exc())
    await asyncio.sleep(SLEEP_TIME)
