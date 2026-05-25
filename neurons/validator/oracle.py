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


import argparse
import time

# Bittensor
import bittensor as bt

# import base validator class which takes care of most of the boilerplate
from tensorusd.base.validator import BaseValidatorNeuron

# Bittensor Validator Template:
from tensorusd.utils.config import add_validator_args
from tensorusd.validator import forward_mech1

# Auction tracking components
from tensorusd.common.contract import (
    TensorUSDPriceOracleContract,
    create_substrate_interface,
)


class OracleValidator(BaseValidatorNeuron):
    """
    Your validator neuron class. You should use this class to define your validator's behavior. In particular, you should replace the forward function with your own logic.

    This class inherits from the BaseValidatorNeuron class, which in turn inherits from BaseNeuron. The BaseNeuron class takes care of routine tasks such as setting up wallet, subtensor, metagraph, logging directory, parsing config, etc. You can override any of the methods in BaseNeuron if you need to customize the behavior.

    This class provides reasonable default behavior for a validator such as keeping a moving average of the scores of the miners and using them to set weights at the end of each epoch. Additionally, the scores are reset for new hotkeys at the end of each epoch.
    """

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        super().add_args(parser)
        add_validator_args(cls, parser, 1)

    def __init__(self, config=None):
        super(OracleValidator, self).__init__(config=config, mech_id=0)

        self.setup()
        self.is_first_run = True

    def setup(self):
        """Initialize auction tracking components."""

        self.tusd_substrate = create_substrate_interface(self.subtensor.chain_endpoint)

        self.oracle_contract = TensorUSDPriceOracleContract(
            substrate=self.tusd_substrate,
            contract_address=self.config.oracle_contract.address,
            metadata_path="tensorusd/common/abis/tusdt_oracle.json",
            wallet=self.wallet,
        )

    def run(self):
        """Override run to start event listener alongside validator."""
        super().run(mech_id=1)

    async def forward(self):
        """
        Validator forward pass. Consists of:
        - Generating the query
        - Querying the miners
        - Getting the responses
        - Rewarding the miners
        - Updating the scores
        """
        return await forward_mech1(self)


# The main function parses the configuration and runs the validator.
if __name__ == "__main__":
    with OracleValidator() as validator:
        while True:
            bt.logging.info(f"Validator running... {time.time()}")
            time.sleep(300)
