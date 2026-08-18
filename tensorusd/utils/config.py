# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 Opentensor Foundation

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

import os
import subprocess
import argparse
import bittensor as bt
from .logging import setup_events_logger
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()


def is_cuda_available():
    try:
        output = subprocess.check_output(["nvidia-smi", "-L"], stderr=subprocess.STDOUT)
        if "NVIDIA" in output.decode("utf-8"):
            return "cuda"
    except Exception:
        pass
    try:
        output = subprocess.check_output(["nvcc", "--version"]).decode("utf-8")
        if "release" in output:
            return "cuda"
    except Exception:
        pass
    return "cpu"


def check_config(cls, config: "bt.Config"):
    r"""Checks/validates the config namespace object."""
    bt.logging.check_config(config)

    full_path = os.path.expanduser(
        "{}/{}/{}/netuid{}/{}".format(
            config.logging.logging_dir,  # TODO: change from ~/.bittensor/miners to ~/.bittensor/neurons
            config.wallet.name,
            config.wallet.hotkey,
            config.netuid,
            config.neuron.name,
        )
    )
    config.neuron.full_path = os.path.expanduser(full_path)
    if not os.path.exists(config.neuron.full_path):
        os.makedirs(config.neuron.full_path, exist_ok=True)

    if not config.neuron.dont_save_events:
        # Add custom event logger for the events.
        events_logger = setup_events_logger(
            config.neuron.full_path, config.neuron.events_retention_size
        )
        bt.logging.register_primary_logger(events_logger.name)


def is_required_arg(
    key: str, required_both: bool, required_mechid: int = 0, current_mech_id: int = 0
):
    if required_both:
        return os.getenv(key) is None
    if current_mech_id != required_mechid:
        return False
    return os.getenv(key) is None


def add_args(cls, parser):
    """
    Adds relevant arguments to the parser for operation.
    """

    parser.add_argument("--netuid", type=int, help="Subnet netuid", default=315)

    parser.add_argument(
        "--neuron.device",
        type=str,
        help="Device to run on.",
        default=is_cuda_available(),
    )

    parser.add_argument(
        "--neuron.epoch_length",
        type=int,
        help="The default epoch length (how often we set weights, measured in 12 second blocks).",
        default=100,
    )

    parser.add_argument(
        "--mock",
        action="store_true",
        help="Mock neuron and all network components.",
        default=False,
    )

    parser.add_argument(
        "--neuron.events_retention_size",
        type=str,
        help="Events retention size.",
        default=2 * 1024 * 1024 * 1024,  # 2 GB
    )

    parser.add_argument(
        "--neuron.dont_save_events",
        action="store_true",
        help="If set, we dont save events to a log file.",
        default=False,
    )

    parser.add_argument(
        "--wandb.off",
        action="store_true",
        help="Turn off wandb.",
        default=False,
    )

    parser.add_argument(
        "--wandb.offline",
        action="store_true",
        help="Runs wandb in offline mode.",
        default=False,
    )

    parser.add_argument(
        "--wandb.notes",
        type=str,
        help="Notes to add to the wandb run.",
        default="",
    )
    parser.add_argument(
        "--mechid",
        type=int,
        help="Mechanism ID to run. 0 = Liquidator, 1 = Agent",
        default=is_required_arg("MECH_ID", True),
    )


def add_miner_args(cls, parser, mech_id: int = 0):
    """Add miner specific arguments to the parser."""

    parser.add_argument(
        "--neuron.name",
        type=str,
        help="Trials for this neuron go in neuron.root / (wallet_cold - wallet_hot) / neuron.name. ",
        default="miner",
    )

    parser.add_argument(
        "--blacklist.force_validator_permit",
        action="store_true",
        help="If set, we will force incoming requests to have a permit.",
        default=False,
    )

    parser.add_argument(
        "--blacklist.allow_non_registered",
        action="store_true",
        help="If set, miners will accept queries from non registered entities. (Dangerous!)",
        default=False,
    )

    parser.add_argument(
        "--wandb.project_name",
        type=str,
        default="template-miners",
        help="Wandb project to log to.",
    )

    parser.add_argument(
        "--wandb.entity",
        type=str,
        default="opentensor-dev",
        help="Wandb entity to log to.",
    )

    # Contract address arguments
    parser.add_argument(
        "--auction_contract.address",
        type=str,
        help="TensorUSD Auction contract address (SS58). Required for auction bidding.",
        default=os.getenv("AUCTION_CONTRACT_ADDRESS", None),
        required=is_required_arg("AUCTION_CONTRACT_ADDRESS", False, 0, mech_id),
    )
    parser.add_argument(
        "--oracle_contract.address",
        type=str,
        help="TensorUSD price oracle contract address (SS58). Required for auction bidding.",
        default=os.getenv("ORACLE_CONTRACT_ADDRESS", None),
        required=is_required_arg("ORACLE_CONTRACT_ADDRESS", False, 0, mech_id),
    )
    parser.add_argument(
        "--vault_contract.address",
        type=str,
        help="TensorUSD Vault contract address (SS58). Required for collateral price.",
        default=os.getenv("VAULT_CONTRACT_ADDRESS", None),
        required=is_required_arg("VAULT_CONTRACT_ADDRESS", False, 0, mech_id),
    )
    # Bidding strategy arguments
    parser.add_argument(
        "--bid.initial_percentage",
        type=float,
        help="Initial bid as percentage of collateral value (0.0-1.0).",
        default=0.11,  # base is 11%, so make sure it's always above this for initial bid
    )

    parser.add_argument(
        "--bid.increment_rate",
        type=float,
        help="Bid increment rate when outbid (0.0-1.0).",
        default=0.0005,
    )

    parser.add_argument(
        "--bid.max_percentage",
        type=float,
        help="Maximum bid as percentage of collateral value (0.0-1.0).",
        default=0.3,
    )

    parser.add_argument(
        "--bid.max_absolute",
        type=int,
        help="Absolute maximum bid in token base units (optional).",
        default=None,
    )

    parser.add_argument(
        "--bid.min_profit_margin",
        type=float,
        help="Minimum profit margin required to bid (0.0-1.0).",
        default=0.0002,
    )

    # TUSDT ERC20 token arguments
    parser.add_argument(
        "--tusdt.address",
        type=str,
        help="TUSDT ERC20 token contract address (SS58). Required for auction bidding.",
        default=os.getenv("TOKEN_CONTRACT_ADDRESS", None),
        required=is_required_arg("TOKEN_CONTRACT_ADDRESS", False, 0, mech_id),
    )

    parser.add_argument(
        "--tusdt.approval_amount",
        type=int,
        help="Amount to approve for auction contract (0 = max uint64).",
        default=0,
    )

    parser.add_argument(
        "--coldkey.password",
        type=str,
        help="coldkey password",
        default=os.getenv("COLDKEY_PASSWORD", None),
        required=is_required_arg("COLDKEY_PASSWORD", True),
    )


def add_validator_args(cls, parser, mech_id: int = 0):
    """Add validator specific arguments to the parser."""

    parser.add_argument(
        "--neuron.name",
        type=str,
        help="Trials for this neuron go in neuron.root / (wallet_cold - wallet_hot) / neuron.name. ",
        default="validator",
    )

    parser.add_argument(
        "--neuron.timeout",
        type=float,
        help="The timeout for each forward call in seconds.",
        default=10,
    )

    parser.add_argument(
        "--neuron.num_concurrent_forwards",
        type=int,
        help="The number of concurrent forwards running at any time.",
        default=1,
    )

    parser.add_argument(
        "--neuron.sample_size",
        type=int,
        help="The number of miners to query in a single step.",
        default=50,
    )

    parser.add_argument(
        "--neuron.disable_set_weights",
        action="store_true",
        help="Disables setting weights.",
        default=False,
    )

    parser.add_argument(
        "--neuron.moving_average_alpha",
        type=float,
        help="Moving average alpha parameter, how much to add of the new observation.",
        default=0.1,
    )

    parser.add_argument(
        "--neuron.axon_off",
        "--axon_off",
        action="store_true",
        # Note: the validator needs to serve an Axon with their IP or they may
        #   be blacklisted by the firewall of serving peers on the network.
        help="Set this flag to not attempt to serve an Axon.",
        default=False,
    )

    parser.add_argument(
        "--neuron.vpermit_tao_limit",
        type=int,
        help="The maximum number of TAO allowed to query a validator with a vpermit.",
        default=4096,
    )

    parser.add_argument(
        "--wandb.project_name",
        type=str,
        help="The name of the project where you are sending the new run.",
        default="template-validators",
    )

    parser.add_argument(
        "--wandb.entity",
        type=str,
        help="The name of the project where you are sending the new run.",
        default="opentensor-dev",
    )
    parser.add_argument(
        "--auction_contract.address",
        type=str,
        help="TensorUSD Auction contract address (SS58). Required for auction bidding.",
        default=os.getenv("AUCTION_CONTRACT_ADDRESS", None),
        required=is_required_arg("AUCTION_CONTRACT_ADDRESS", False, 0, mech_id),
    )
    parser.add_argument(
        "--oracle_contract.address",
        type=str,
        help="TensorUSD price oracle contract address (SS58). Required for auction bidding.",
        default=os.getenv("ORACLE_CONTRACT_ADDRESS", None),
        required=is_required_arg("ORACLE_CONTRACT_ADDRESS", False, 0, mech_id),
    )
    parser.add_argument(
        "--vault_contract.address",
        type=str,
        help="TensorUSD Vault contract address (SS58). Required for collateral price.",
        default=os.getenv("VAULT_CONTRACT_ADDRESS", None),
        required=is_required_arg("VAULT_CONTRACT_ADDRESS", False, 1, mech_id),
    )
    parser.add_argument(
        "--agent.backend_url",
        type=str,
        help="Backend URL for TensorUSD Agent. Required for running agent validator.",
        default=os.getenv("TENSORUSD_SN_BACKEND_URL", None),
        required=is_required_arg("ORACLE_CONTRACT_ADDRESS", False, 1, mech_id),
    )
    parser.add_argument(
        "--agent.http_timeout",
        type=int,
        help="HTTP timeout for backend url connection",
        default=30,
    )
    parser.add_argument(
        "--agent.validator_poll_interval",
        type=int,
        help="Interval (in seconds) for agent validator to check backend for scoring or evaluation.",
        default=30,
    )
    parser.add_argument(
        "--agent.weight_interval_blocks",
        type=int,
        help="Interval (in blocks) to set weight for winner agent",
        default=300,
    )
    parser.add_argument(
        "--agent.weight_monitor_interval",
        type=int,
        help="Interval in seconds, to log on-chain weights.",
        default=900,
    )
    parser.add_argument(
        "--agent.ground_truth_dir",
        type=Path,
        help="Path where validator saves ground truth.",
        default="ground-truth",
    )
    parser.add_argument(
        "--agent.best_agent_dir",
        type=Path,
        help="Path to store the best agent for comparison",
        default=".TENSORUSD_cache/best_agent",
    )
    parser.add_argument(
        "--agent.scored_cache_path",
        type=Path,
        help="Path to store the best agent for comparison",
        default=".TENSORUSD_cache/scored_submissions.json",
    )
    parser.add_argument(
        "--agent.scored_cache_max_size",
        type=int,
        help="Max info stored as cache",
        default=100,
    )
    parser.add_argument(
        "--agent.best_agent_poll_interval",
        type=int,
        help="Interval to look for best available agent in backend server.",
        default=600,
    )
    parser.add_argument(
        "--agent.sandbox_image",
        type=str,
        help="Sandbox image agent validator use to run the agent files.",
        default="tensorusd-sandbox:latest",
    )
    parser.add_argument(
        "--agent.sandbox_workdir",
        type=Path,
        help="Working directory for sandbox",
        default="/tmp/TENSORUSD_sandbox",
    )
    parser.add_argument(
        "--agent.sandbox_log_dir",
        type=Path,
        help="Directory for logging the agent run",
        default="logs/sandbox",
    )
    parser.add_argument(
        "--agent.sandbox_allowed_models",
        type=str,
        help="Comma-separated list of allowed OpenAI models in the sandbox.",
        default="gpt-5.6-terra,gpt-5.6-luna,gpt-5.4,gpt-5.4-mini,gpt-5.4-nano,gpt-5-mini,gpt-5-nano,gpt-4.1,gpt-4.1-mini,gpt-4o-mini",
    )
    parser.add_argument(
        "--agent.allowed_hosts",
        type=str,
        help="Allowed external hosts inside sandbox",
        default="api.openai.com,*.openai.com,openai.com",
    )
    parser.add_argument(
        "--agent.logfile_max_bytes",
        type=int,
        help="Max size of the logfile before making a new logfile to store logs",
        default=10 * 1024 * 1024,
    )
    parser.add_argument(
        "--agent.logs_backup_count",
        type=int,
        help="Max numbers of logfile to save.",
        default=7,
    )
    parser.add_argument(
        "--agent.sandbox_timeout",
        type=int,
        help="Maximum amount of time (in seconds) for sandbox to run an agent.",
        default= 15 * 60,
    )
    parser.add_argument(
        "--agent.memory_limit",
        type=str,
        help="Max numbers of logfile to save.",
        default="8g",
    )
    parser.add_argument(
        "--agent.cpu_limit",
        type=int,
        help="Maximum amount of vcpu to run an agent.",
        default=4,
    )
    parser.add_argument(
        "--agent.scoring_poll_interval",
        type=int,
        help="Interval (in seconds) to check unscored predictions.",
        default=900,
    )
    parser.add_argument(
        "--agent.plagiarism_threshold",
        type=float,
        help="How same can a new agent be in comparison to current best",
        default=0.95,
    )


def config(cls):
    """
    Returns the configuration object specific to this miner or validator after adding relevant arguments.
    """
    parser = argparse.ArgumentParser()
    bt.Wallet.add_args(parser)
    bt.Subtensor.add_args(parser)
    bt.logging.add_args(parser)
    bt.Axon.add_args(parser)
    cls.add_args(parser)
    return bt.Config(parser)
