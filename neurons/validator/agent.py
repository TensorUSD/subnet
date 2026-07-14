# The MIT License (MIT)
# Copyright © 2026 Your Name

import argparse
import sys
import os

from pathlib import Path

# Fix potential import resolution paths before bringing in local modules
root_path = str(Path(__file__).resolve().parents[2])
if root_path not in sys.path:
    sys.path.insert(0, root_path)

import bittensor as bt

# Base validator framework
from tensorusd.base.validator import BaseValidatorNeuron

# Settings and Core Agent Components
from tensorusd.auth.config import settings
from tensorusd.utils.logging import get_logger
from tensorusd.utils.core import ValidatorCore

log = get_logger(__name__)


class AgentValidator(BaseValidatorNeuron):
    """
    Agent validator neuron class. Formatted to follow the BaseValidatorNeuron template
    while adapting the runtime layer to match the external ValidatorCore backend engine.
    """

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        """Register custom CLI arguments required for this validator."""
        super().add_args(parser)
        # Standard Bittensor arguments are handled automatically by BaseValidatorNeuron

    def __init__(self, config=None):
        # BaseValidatorNeuron manages parsing config, creating wallets, subtensor, and metagraphs.
        # mech_id=1 mfor agent validation
        super(AgentValidator, self).__init__(config=config, mech_id=1)

        # Handle network configuration environment variables if passed via CLI configurations
        if self.config.subtensor and self.config.subtensor.get("network"):
            os.environ["BITTENSOR_NETWORK"] = self.config.subtensor.network

        # Run registration and permit verification steps
        self.verify_validator_permit()

        # Initialize internal Agent components
        self.setup()

    def verify_validator_permit(self):
        """Verifies if the loaded validator hotkey holds registration and network validation permit."""
        log.info("Checking validator permit on netuid %d ...", settings.netuid)
        try:
            hotkey_ss58 = self.wallet.hotkey.ss58_address

            if hotkey_ss58 not in self.metagraph.hotkeys:
                log.error(
                    "  Hotkey %s is not registered on netuid %d.\n"
                    "  Register first:  btcli subnet register --netuid %d",
                    hotkey_ss58,
                    settings.netuid,
                    settings.netuid,
                )
                sys.exit(1)

            uid = self.metagraph.hotkeys.index(hotkey_ss58)
            has_permit = bool(self.metagraph.validator_permit[uid])

            if not has_permit:
                log.error(
                    "  Hotkey %s (uid=%d) does not have a validator permit on netuid %d.\n"
                    "  Please make sure you are using validator hotkey.\n",
                    hotkey_ss58,
                    uid,
                    settings.netuid,
                )
                sys.exit(1)

            log.info("Validator permit confirmed — uid=%d hotkey=%s", uid, hotkey_ss58)

        except SystemExit:
            raise
        except Exception as exc:
            # Prevent chain communication hitches from crashing full node starts
            log.warning(
                "Could not verify validator permit (chain unreachable?): %s\n"
                "  Proceeding anyway — backend will reject requests if permit is missing.",
                exc,
            )

    def setup(self):
        """Pre-configure backend engine environments."""
        log.info(
            "Starting TensorUSD Agent validator\n"
            "  network        : %s\n"
            "  netuid         : %d\n"
            "  hotkey         : %s\n"
            "  backend        : %s\n",
            settings.network,
            settings.netuid,
            self.wallet.hotkey.ss58_address,
            settings.backend_url,
        )

        # Hand off our pre-initialized wallet instance to your backend tracking layer
        self.core = ValidatorCore(wallet=self.wallet, validator=self,mechid=1)

    def run(self):
        """
        Overrides BaseValidatorNeuron.run() to block on your custom internal backend
        instead of entering the concurrent loop forward pass routine.
        """
        # Ensure underlying system synchronization state updates occur once before launching
        # self.sync(mechid=mech_id)

        log.info(
            "Validator core successfully linked. Initializing Agent loop execution."
        )
        try:
            # Execution hooks into your dedicated background tracking loops here
            self.core.start()
            # super().run(mech_id=1)
        except (KeyboardInterrupt, SystemExit):
            log.info("Validator received a kill signal. Stopping context execution.")

    async def forward(self):
        """
        Abstract signature match for BaseValidatorNeuron.
        Unused since execution blocks inside `self.core.start()`.
        """
        pass


# Main runtime execution routine
if __name__ == "__main__":
    validator = AgentValidator()
    validator.run()
