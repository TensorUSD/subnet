import argparse
import time
import sys

import bittensor as bt

from tensorusd.utils.core import ValidatorCore
from tensorusd.base.validator import BaseValidatorNeuron

from tensorusd.utils.config import add_validator_args
from tensorusd.validator.forward_mech1 import forward_mech1
from tensorusd.utils.logging import get_logger

from tensorusd.common.contract import create_substrate_interface

log = get_logger(__name__)


class AgentValidator(BaseValidatorNeuron):
    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        super().add_args(parser)
        add_validator_args(cls, parser, 1)

    def __init__(self, config=None):
        super(AgentValidator, self).__init__(config=config, mech_id=1)

        self.setup()
        self.is_first_run = True
        self.verify_validator_permit()

    def verify_validator_permit(self):
        """Verifies if the loaded validator hotkey holds registration and network validation permit."""
        bt.logging.info(
            "Checking validator permit on netuid %d ...", self.config.netuid
        )
        try:
            hotkey_ss58 = self.wallet.hotkey.ss58_address

            if hotkey_ss58 not in self.metagraph.hotkeys:
                bt.logging.error(
                    "  Hotkey %s is not registered on netuid %d.\n"
                    "  Register first:  btcli subnet register --netuid %d",
                    hotkey_ss58,
                    self.config.netuid,
                    self.config.netuid,
                )
                sys.exit(1)

            uid = self.metagraph.hotkeys.index(hotkey_ss58)
            has_permit = bool(self.metagraph.validator_permit[uid])

            if not has_permit:
                bt.logging.error(
                    "%s  Hotkey %s (uid=%d) does not have a validator permit on netuid %d.\n"
                    "%s  Please make sure you are using validator hotkey.\n",
                    hotkey_ss58,
                    uid,
                    self.netuid,
                )
                sys.exit(1)

            bt.logging.info(
                "Validator permit confirmed — uid=%d hotkey=%s", uid, hotkey_ss58
            )

        except Exception as exc:
            bt.logging.warning(
                "Could not verify validator permit (chain unreachable?): %s\n"
                "  Proceeding anyway — backend will reject requests if permit is missing.",
                exc,
            )

    def setup(self):

        self.vault_substrate = create_substrate_interface(self.subtensor.chain_endpoint)

        self.agent_vali = ValidatorCore(
            wallet=self.wallet,
            netuid=self.config.netuid,
            network=self.config.subtensor.network,
            rpc_endpoint=self.config.subtensor.chain_endpoint,
            scored_cache_path = self.config.agent.scored_cache_path,
            scored_cache_max_size = self.config.agent.scored_cache_max_size,
            validator_poll_interval = self.config.agent.validator_poll_interval,
            scoring_poll_interval = self.config.agent.scoring_poll_interval,
            plagiarism_threshold = self.config.agent.plagiarism_threshold,
            vault_address=self.config.vault_contract.address,
            vault_metadata_path="tensorusd/common/abis/tusdt_vault.json",
        )

        self.agent_vali.start_background()

    def run(self):
        super().run(mech_id=1)

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.agent_vali.stop()
        except Exception as e:
            bt.logging.debug(f"Error stopping agent_vali: {e}")
        try:
            self.vault_substrate.close()
        except Exception as e:
            bt.logging.debug(f"Error closing vault substrate: {e}")
        super().__exit__(exc_type, exc_value, traceback)

    async def forward(self):
        return await forward_mech1(self)


if __name__ == "__main__":
    with AgentValidator() as validator:
        while True:
            log.info(f"Agent Validator running.....{time.time()}")
            time.sleep(600)
