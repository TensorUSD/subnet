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
import typing
import bittensor as bt

# Bittensor Miner tensorusd:

# import base miner class which takes care of most of the boilerplate
from tensorusd import protocol
from tensorusd.liquidator import AuctionUnionEvent, AuctionEventType
from tensorusd.base.miner import BaseMinerNeuron


from tensorusd.liquidator.config import MinerBidConfig
from tensorusd.common.contract import (
    TensorUSDAuctionContract,
    TensorUSDPriceOracleContract,
    TensorUSDVaultContract,
    create_substrate_interface,
)
from tensorusd.liquidator.erc20 import TUSDTContract, MAX_APPROVAL
from tensorusd.liquidator.event_listener import AuctionEventListener
from tensorusd.liquidator.bidding import BiddingStrategy
from tensorusd.liquidator.auction_manager import MinerAuctionManager
import concurrent.futures

from tensorusd.utils.config import add_miner_args


class LiquidationMiner(BaseMinerNeuron):
    """
    Your miner neuron class. You should use this class to define your miner's behavior. In particular, you should replace the forward function with your own logic. You may also want to override the blacklist and priority functions according to your needs.

    This class inherits from the BaseMinerNeuron class, which in turn inherits from BaseNeuron. The BaseNeuron class takes care of routine tasks such as setting up wallet, subtensor, metagraph, logging directory, parsing config, etc. You can override any of the methods in BaseNeuron if you need to customize the behavior.

    This class provides reasonable default behavior for a miner such as blacklisting unrecognized hotkeys, prioritizing requests based on stake, and forwarding requests to the forward function. If you need to define custom
    """

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        super().add_args(parser)
        add_miner_args(cls, parser, 0)

    def __init__(self, config=None):
        super(LiquidationMiner, self).__init__(config=config, mech_id=0)
        self.event_listener_substrate = create_substrate_interface(
            self.subtensor.chain_endpoint
        )
        self.auction_substrate = create_substrate_interface(
            self.subtensor.chain_endpoint
        )
        self.oracle_substrate = create_substrate_interface(
            self.subtensor.chain_endpoint
        )

        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self.oracle_contract = TensorUSDPriceOracleContract(
            substrate=self.oracle_substrate,
            contract_address=self.config.oracle_contract.address,
            metadata_path="tensorusd/common/abis/tusdt_oracle.json",
            wallet=self.wallet,
        )

    def _init_auction_system(self):
        # Initialize bid config from CLI args
        self.bid_config = MinerBidConfig(
            initial_bid_percentage=self.config.bid.initial_percentage,
            bid_increment_rate=self.config.bid.increment_rate,
            max_bid_percentage=self.config.bid.max_percentage,
            max_bid_absolute=self.config.bid.max_absolute,
            min_profit_margin=self.config.bid.min_profit_margin,
        )

        # Initialize auction contract interface
        self.auction_contract = TensorUSDAuctionContract(
            substrate=self.auction_substrate,
            contract_address=self.config.auction_contract.address,
            metadata_path="tensorusd/common/abis/tusdt_auction.json",
            wallet=self.wallet,
        )

        # Initialize vault contract interface (for collateral price)
        self.vault_contract = TensorUSDVaultContract(
            substrate=self.auction_substrate,
            contract_address=self.config.vault_contract.address,
            metadata_path="tensorusd/common/abis/tusdt_vault.json",
            wallet=self.wallet,
        )

        self.tusdt_contract = TUSDTContract(
            substrate=self.auction_substrate,
            contract_address=self.config.tusdt.address,
            metadata_path="tensorusd/common/abis/tusdt_erc20.json",
            wallet=self.wallet,
        )

        approval_amount = self.config.tusdt.approval_amount
        if approval_amount == 0:
            approval_amount = MAX_APPROVAL
        # Initialize bidding strategy
        self.strategy = BiddingStrategy(self.bid_config)

        # Initialize auction manager
        self.auction_manager = MinerAuctionManager(
            auction_contract=self.auction_contract,
            vault_contract=self.vault_contract,
            oracle_contract=self.oracle_contract,
            strategy=self.strategy,
            wallet=self.wallet,
            tusdt_contract=self.tusdt_contract,
            approval_amount=approval_amount,
        )

        # Initialize event listener
        self.event_listener = AuctionEventListener(
            substrate=self.event_listener_substrate,
            contract_address=self.config.auction_contract.address,
            metadata_path="tensorusd/common/abis/tusdt_auction.json",
            callback=self._handle_auction_event,
        )

    def _handle_auction_event(self, event: AuctionUnionEvent | None, block_number: int):
        """
        Callback from event listener - runs in listener thread.

        Args:
            event: Decoded auction event
        """
        if not event:
            return
        bt.logging.info(
            f"Auction event: {event.event_type.value} - Auction ID: {event.auction_id} - Block: {block_number}"
        )
        self.executor.submit(self._process_event, event)

    def _process_event(self, event: AuctionUnionEvent):
        try:
            if event.event_type == AuctionEventType.CREATED:
                self.auction_manager.handle_auction_created(event)
            elif event.event_type == AuctionEventType.BID_PLACED:
                self.auction_manager.handle_bid_placed(event)
            elif event.event_type == AuctionEventType.FINALIZED:
                self.auction_manager.handle_auction_finalized(event)
        except Exception as e:
            bt.logging.error(
                f"Error handling auction event {event.event_type.value} - {event.auction_id}: {e}",
                exc_info=True,
            )

    def run(self):
        """Override run to start event listener alongside axon."""
        bt.logging.info("Mining in mech 0")
        self._init_auction_system()
        bt.logging.info("Catching up on active auctions...")
        self.executor.submit(self.auction_manager.sync_active_auctions)
        # Start listening for new events
        self.event_listener.run_in_executor(self.executor)

        # Run normal miner operation (axon serving, metagraph sync)
        super().run(mech_id=0)

    def __exit__(self, exc_type, exc_value, traceback):
        self.event_listener.stop()
        self.executor.shutdown(wait=True, cancel_futures=False)

        # Close substrate connections
        try:
            self.event_listener_substrate.close()
        except Exception as e:
            bt.logging.debug(f"Error closing event listener substrate: {e}")

        try:
            self.auction_substrate.close()
        except Exception as e:
            bt.logging.debug(f"Error closing auction substrate: {e}")

        super().__exit__(exc_type, exc_value, traceback)

    async def blacklist(self, synapse: protocol.Dummy) -> typing.Tuple[bool, str]:
        """
        Determines whether an incoming request should be blacklisted and thus ignored. Your implementation should
        define the logic for blacklisting requests based on your needs and desired security parameters.

        Blacklist runs before the synapse data has been deserialized (i.e. before synapse.data is available).
        The synapse is instead contracted via the headers of the request. It is important to blacklist
        requests before they are deserialized to avoid wasting resources on requests that will be ignored.

        Args:
            synapse (tensorusd.protocol.Dummy): A synapse object constructed from the headers of the incoming request.

        Returns:
            Tuple[bool, str]: A tuple containing a boolean indicating whether the synapse's hotkey is blacklisted,
                            and a string providing the reason for the decision.

        This function is a security measure to prevent resource wastage on undesired requests. It should be enhanced
        to include checks against the metagraph for entity registration, validator status, and sufficient stake
        before deserialization of synapse data to minimize processing overhead.

        Example blacklist logic:
        - Reject if the hotkey is not a registered entity within the metagraph.
        - Consider blacklisting entities that are not validators or have insufficient stake.

        In practice it would be wise to blacklist requests from entities that are not validators, or do not have
        enough stake. This can be checked via metagraph.S and metagraph.validator_permit. You can always attain
        the uid of the sender via a metagraph.hotkeys.index( synapse.dendrite.hotkey ) call.

        Otherwise, allow the request to be processed further.
        """

        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            bt.logging.warning("Received a request without a dendrite or hotkey.")
            return True, "Missing dendrite or hotkey"

        # TODO(developer): Define how miners should blacklist requests.
        uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
        if (
            not self.config.blacklist.allow_non_registered
            and synapse.dendrite.hotkey not in self.metagraph.hotkeys
        ):
            # Ignore requests from un-registered entities.
            bt.logging.trace(
                f"Blacklisting un-registered hotkey {synapse.dendrite.hotkey}"
            )
            return True, "Unrecognized hotkey"

        if self.config.blacklist.force_validator_permit:
            # If the config is set to force validator permit, then we should only allow requests from validators.
            if not self.metagraph.validator_permit[uid]:
                bt.logging.warning(
                    f"Blacklisting a request from non-validator hotkey {synapse.dendrite.hotkey}"
                )
                return True, "Non-validator hotkey"

        bt.logging.trace(
            f"Not Blacklisting recognized hotkey {synapse.dendrite.hotkey}"
        )
        return False, "Hotkey recognized!"

    async def priority(self, synapse: protocol.Dummy) -> float:
        """
        The priority function determines the order in which requests are handled. More valuable or higher-priority
        requests are processed before others. You should design your own priority mechanism with care.

        This implementation assigns priority to incoming requests based on the calling entity's stake in the metagraph.

        Args:
            synapse (tensorusd.protocol.Dummy): The synapse object that contains metadata about the incoming request.

        Returns:
            float: A priority score derived from the stake of the calling entity.

        Miners may receive messages from multiple entities at once. This function determines which request should be
        processed first. Higher values indicate that the request should be processed first. Lower values indicate
        that the request should be processed later.

        Example priority logic:
        - A higher stake results in a higher priority value.
        """
        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            bt.logging.warning("Received a request without a dendrite or hotkey.")
            return 0.0

        # TODO(developer): Define how miners should prioritize requests.
        caller_uid = self.metagraph.hotkeys.index(
            synapse.dendrite.hotkey
        )  # Get the caller index.
        priority = float(
            self.metagraph.S[caller_uid]
        )  # Return the stake as the priority.
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: {priority}"
        )
        return priority


# This is the main function, which runs the miner.
if __name__ == "__main__":
    with LiquidationMiner() as miner:
        while True:
            bt.logging.info(f"Miner running... {time.time()}")
            time.sleep(300)
