"""
ERC20 Token Contract Interface for TUSDT.

Provides methods for checking allowance and approving spender.
"""

from typing import Optional

from substrateinterface import SubstrateInterface, Keypair
from substrateinterface.contracts import ContractMetadata, ContractInstance

import bittensor as bt


# Max uint64 value for unlimited approval
MAX_APPROVAL = 2**64 - 1


class TUSDTContract:
    """
    Interface for interacting with the TUSDT ERC20 token contract.

    Provides methods for:
    - Checking token balance
    - Checking allowance
    - Approving spender
    """

    def __init__(
        self,
        substrate: SubstrateInterface,
        contract_address: str,
        metadata_path: str,
        wallet: bt.Wallet,
    ):
        """
        Initialize ERC20 contract interface.

        Args:
            substrate: Shared SubstrateInterface instance
            contract_address: SS58 address of the TUSDT contract
            metadata_path: Path to tusdt_erc20.json metadata file
            wallet: Wallet for signing transactions
        """
        self.substrate = substrate
        self.contract_address = contract_address
        self.metadata_path = metadata_path
        self.wallet = wallet

        # Load contract metadata
        self.metadata = ContractMetadata.create_from_file(
            metadata_file=metadata_path,
            substrate=self.substrate,
        )

        # Create contract instance for calls
        self.contract = ContractInstance(
            contract_address=contract_address,
            metadata=self.metadata,
            substrate=self.substrate,
        )

        bt.logging.info(f"TUSDT ERC20 contract initialized at {contract_address}")

    def get_balance(self, owner: str) -> int:
        """
        Get token balance for an account.

        Args:
            owner: SS58 address of token owner

        Returns:
            Token balance or 0 if error
        """
        try:
            result = self.contract.read(
                keypair=self.wallet.coldkey,
                method="balance_of",
                args={"owner": owner},
            )

            data = result.contract_result_data.value_object
            if data and data[0] == "Ok":
                return data[1].value
            return 0
        except Exception as e:
            bt.logging.error(f"Error getting balance: {e}")
            return 0

    def get_allowance(self, owner: str, spender: str) -> int:
        """
        Get allowance for spender to spend owner's tokens.

        Args:
            owner: SS58 address of token owner
            spender: SS58 address of spender (auction contract)

        Returns:
            Allowance amount or 0 if error
        """
        try:
            result = self.contract.read(
                keypair=self.wallet.coldkey,
                method="allowance",
                args={"owner": owner, "spender": spender},
            )

            data = result.contract_result_data.value_object
            if data and data[0] == "Ok":
                return data[1].value
            return 0
        except Exception as e:
            bt.logging.error(f"Error getting allowance: {e}")
            return 0

    def approve(
        self,
        spender: str,
        value: int,
        keypair: Keypair,
    ) -> Optional[str]:
        """
        Approve spender to spend tokens.

        Args:
            spender: SS58 address of spender to approve
            value: Amount to approve
            keypair: Keypair to sign the transaction

        Returns:
            Transaction hash if successful, None otherwise
        """
        try:
            args = {"spender": spender, "value": value}

            # First, predict gas required
            gas_predict_result = self.contract.read(
                keypair=keypair,
                method="approve",
                args=args,
            )

            # Execute with predicted gas limit
            receipt = self.contract.exec(
                keypair=keypair,
                method="approve",
                args=args,
                gas_limit=gas_predict_result.gas_required,
            )

            if receipt.is_success:
                bt.logging.success(
                    f"Approval successful: spender={spender}, value={value}, "
                    f"tx={receipt.extrinsic_hash}"
                )
                return receipt.extrinsic_hash
            else:
                bt.logging.error(f"Approval failed: {receipt.error_message}")
                return None

        except Exception as e:
            bt.logging.error(f"Error approving: {e}")
            return None

    def ensure_allowance(
        self,
        spender: str,
        required_amount: int,
        approval_amount: Optional[int] = None,
    ) -> bool:
        """
        Ensure sufficient allowance for spender, approving if needed.

        Args:
            spender: SS58 address of spender (auction contract)
            required_amount: Minimum allowance needed
            approval_amount: Amount to approve if needed (defaults to MAX_APPROVAL)

        Returns:
            True if allowance is sufficient or approval succeeded, False otherwise
        """
        owner = self.wallet.coldkey.ss58_address

        # Check current allowance
        current_allowance = self.get_allowance(owner, spender)
        bt.logging.debug(
            f"Current allowance for {spender}: {current_allowance}, required: {required_amount}"
        )

        if current_allowance >= required_amount:
            return True

        # Need to approve
        if approval_amount is None:
            approval_amount = MAX_APPROVAL

        bt.logging.info(
            f"Insufficient allowance ({current_allowance} < {required_amount}). "
            f"Approving {approval_amount}..."
        )

        tx_hash = self.approve(
            spender=spender,
            value=approval_amount,
            keypair=self.wallet.coldkey,
        )

        return tx_hash is not None
