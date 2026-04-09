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


import logging
import time

# Bittensor
import bittensor as bt

# import base validator class which takes care of most of the boilerplate
from tensorusd.base.validator import BaseValidatorNeuron

# Bittensor Validator Template:
from tensorusd.utils.subnet import get_dynamic_info
from tensorusd.validator import forward, forward_mech1

# Auction tracking components
from tensorusd.auction.contract import (
    TensorUSDAuctionContract,
    TensorUSDPriceOracle,
    create_substrate_interface,
)
from tensorusd.validator.db import init_db
from tensorusd.validator.event_listener import ValidatorEventListener


class Validator(BaseValidatorNeuron):
    """
    Your validator neuron class. You should use this class to define your validator's behavior. In particular, you should replace the forward function with your own logic.

    This class inherits from the BaseValidatorNeuron class, which in turn inherits from BaseNeuron. The BaseNeuron class takes care of routine tasks such as setting up wallet, subtensor, metagraph, logging directory, parsing config, etc. You can override any of the methods in BaseNeuron if you need to customize the behavior.

    This class provides reasonable default behavior for a validator such as keeping a moving average of the scores of the miners and using them to set weights at the end of each epoch. Additionally, the scores are reset for new hotkeys at the end of each epoch.
    """

    def __init__(self, config=None):
        super(Validator, self).__init__(config=config)

        bt.logging.info("load_state()")
        self.load_state()

        # Initialize auction tracking system
        self.setup()
        self.is_first_run = True

    def setup(self):
        """Initialize auction tracking components."""

        # Initialize SQLite database
        self.db_session_factory = init_db()

        # Initialize substrate interface ONCE and share with all components
        self.tusd_substrate = create_substrate_interface(self.subtensor.chain_endpoint)

        # Initialize auction contract
        self.auction_contract = TensorUSDAuctionContract(
            substrate=self.tusd_substrate,
            contract_address=self.config.auction_contract.address,
            metadata_path="tensorusd/abis/tusdt_auction.json",
            wallet=self.wallet,
        )

        # Initialize event listener with shared substrate (stores events in DB)
        self.event_listener = ValidatorEventListener(
            substrate=self.tusd_substrate,
            contract_address=self.config.auction_contract.address,
            metadata_path="tensorusd/abis/tusdt_auction.json",
            db_session_factory=self.db_session_factory,
            auction_contract=self.auction_contract,
        )

        self.oracle_contract = TensorUSDPriceOracle(
            substrate=self.tusd_substrate,
            contract_address=self.config.oracle_contract.address,
            metadata_path="tensorusd/abis/tusdt_oracle.json",
            wallet=self.wallet,
        )

        bt.logging.info(
            f"Auction tracking enabled for contract {self.config.auction_contract.address}"
        )

    def run(self):
        """Override run to start event listener alongside validator."""
        self.event_listener.run_in_background_thread()
        dynamic_info = get_dynamic_info(self.subtensor, self.config.netuid)
        self.event_listener.sync_historical_wins(
            dynamic_info["last_step_block"], self.block
        )
        self.tempo = dynamic_info["tempo"]
        # Run normal validator operation
        super().run()

    def __exit__(self, exc_type, exc_value, traceback):
        self.event_listener.stop_run_thread()
        super().__exit__(exc_type, exc_value, traceback)

    async def forward(self):
        """
        Validator forward pass. Consists of:
        - Generating the query
        - Querying the miners
        - Getting the responses
        - Rewarding the miners
        - Updating the scores
        """
        # TODO(developer): Rewrite this function based on your protocol definition.
        return await forward(self)

    async def forward_mech1(self):
        return await forward_mech1(self)


# The main function parses the configuration and runs the validator.
if __name__ == "__main__":
    with Validator() as validator:
        while True:
            bt.logging.info(f"Validator running... {time.time()}")
            time.sleep(300)
