import asyncio
from typing import TYPE_CHECKING

from tensorusd.validator.reward import calculate_rewards_for_mech1
import bittensor as bt
import numpy as np


if TYPE_CHECKING:
    from neurons.validator import Validator

SLEEP_TIME = 300  # Sleep for 5 minutes between mech1 validations, adjust as needed


async def forward_mech1(self: "Validator"):
    current_round_id = self.oracle_contract.get_current_round_id()
    to_validate_round_id = current_round_id - 1
    if to_validate_round_id == 0:
        bt.logging.info("No rounds to validate yet, burning reward to UID 0")
        rewards, uids = np.array([1.0]), [0]
    else:
        to_validate_round_submissions = self.oracle_contract.get_round_submissions(
            to_validate_round_id
        )
        if len(to_validate_round_submissions) == 0:
            bt.logging.info(
                f"No submissions for round {to_validate_round_id}, burning reward to UID 0"
            )
            rewards, uids = np.array([1.0]), [0]

        to_validate_round_price = self.oracle_contract.get_round_price(
            to_validate_round_id
        )
        rewards, uids = calculate_rewards_for_mech1(
            metagraph=self.metagraph_0,
            submissions=to_validate_round_submissions,
            actual_price=to_validate_round_price,
            burn_uid=0,
            burn_weight_percent=0,
        )
    bt.logging.info(
        f"Rewards: {rewards}, Uids: {uids}, for round {to_validate_round_id}, mechid=1"
    )
    self.update_scores(rewards, uids, 1)
    await asyncio.sleep(SLEEP_TIME)
