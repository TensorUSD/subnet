from dataclasses import dataclass
from typing import Optional, List

from substrateinterface import SubstrateInterface, Keypair
from substrateinterface.contracts import (
    ContractMetadata,
    ContractInstance,
)

import bittensor as bt


@dataclass
class Vault:
    id: int
    owner: str
    collateral_balance: int
    borrowed_token_balance: int
    created_at: int
    last_interest_accrued_at: int


@dataclass
class ContractParams:
    collateral_ratio: int
    liquidation_ratio: int
    interest_rate: int
    liquidation_fee: int
    auction_duration_ms: int


@dataclass
class Auction:
    id: int
    vault_owner: str
    vault_id: int
    collateral_balance: int
    debt_balance: int
    starts_at: int
    ends_at: int
    highest_bidder: Optional[str]
    highest_bid: int
    highest_bid_id: Optional[int]
    bid_count: int
    is_finalized: bool


@dataclass
class Bid:
    id: int
    auction_id: int
    bidder: str
    amount: int


@dataclass
class ActiveAuction:
    auction_id: int
    vault_owner: str
    vault_id: int
    collateral_balance: int
    debt_balance: int
    highest_bid: int
    highest_bidder: Optional[str]
    ends_at: int


def create_substrate_interface(rpc_endpoint: str) -> SubstrateInterface:
    return SubstrateInterface(
        url=rpc_endpoint,
        use_remote_preset=True,
        type_registry={"types": {"Balance": "u64"}},
    )


class TensorUSDVaultContract:
    """
    Interface for interacting with the TensorUSD vault contract.

    Provides methods for:
    - Reading vault data (collateral, debt, etc.)
    - Getting liquidation auction IDs for vaults
    """

    def __init__(
        self,
        substrate: SubstrateInterface,
        contract_address: str,
        metadata_path: str,
        wallet: bt.Wallet,
    ):
        """
        Initialize vault contract interface.

        Args:
            substrate: Shared SubstrateInterface instance
            contract_address: SS58 address of the vault contract
            metadata_path: Path to tusdt_vault.json metadata file
            wallet: Wallet for signing transactions and querying
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

        bt.logging.info(f"TensorUSD vault contract initialized at {contract_address}")

    def get_contract_params(self) -> dict:
        """
        Get contract parameters.
        """
        try:
            result = self.contract.read(
                keypair=self.wallet.hotkey,
                method="get_contract_params",
            )
            data = result.contract_result_data.value_object
            if data and data[0] == "Ok" and data[1]:
                return ContractParams(
                    collateral_ratio=data[1].value["collateral_ratio"],
                    liquidation_ratio=data[1].value["liquidation_ratio"],
                    interest_rate=data[1].value["interest_rate"],
                    liquidation_fee=data[1].value["liquidation_fee"],
                    auction_duration_ms=data[1].value["auction_duration_ms"],
                )
            return None
        except Exception as e:
            bt.logging.error(f"Error getting contract parameters: {e}")
            return None

    def get_vault(self, owner: str, vault_id: int) -> Optional[Vault]:
        """
        Get vault details by owner and vault_id.

        Args:
            owner: SS58 address of vault owner
            vault_id: Vault ID (u32)

        Returns:
            Vault dataclass or None if not found
        """
        try:
            result = self.contract.read(
                keypair=self.wallet.hotkey,
                method="get_vault",
                args={"owner": owner, "vault_id": vault_id},
            )

            data = result.contract_result_data.value_object
            if data and data[0] == "Ok" and data[1]:
                vault_data = data[1].value_object
                return Vault(
                    id=vault_data["id"],
                    owner=vault_data["owner"],
                    collateral_balance=vault_data["collateral_balance"],
                    borrowed_token_balance=vault_data["borrowed_token_balance"],
                    created_at=vault_data["created_at"],
                    last_interest_accrued_at=vault_data["last_interest_accrued_at"],
                )
            return None
        except Exception as e:
            bt.logging.error(f"Error getting vault: {e}")
            return None

    def get_vault_collateral_balance(self, owner: str, vault_id: int) -> Optional[int]:
        """
        Get collateral balance for a vault.

        Args:
            owner: SS58 address of vault owner
            vault_id: Vault ID (u32)

        Returns:
            Collateral balance (u128) or None if not found
        """
        try:
            result = self.contract.read(
                keypair=self.wallet.hotkey,
                method="get_vault_collateral_balance",
                args={"owner": owner, "vault_id": vault_id},
            )

            data = result.contract_result_data.value_object
            if data and data[0] == "Ok":
                return data[1].value
            return None
        except Exception as e:
            bt.logging.error(f"Error getting vault collateral balance: {e}")
            return None

    def get_liquidation_auction_id(self, owner: str, vault_id: int) -> Optional[int]:
        """
        Get liquidation auction ID for a vault.

        Args:
            owner: SS58 address of vault owner
            vault_id: Vault ID (u32)

        Returns:
            Auction ID (u64) or None if no auction
        """
        try:
            result = self.contract.read(
                keypair=self.wallet.hotkey,
                method="get_liquidation_auction_id",
                args={"owner": owner, "vault_id": vault_id},
            )

            data = result.contract_result_data.value_object
            if data and data[0] == "Ok":
                return data[1].value
            return None
        except Exception as e:
            bt.logging.error(f"Error getting liquidation auction ID: {e}")
            return None

    def get_collateral_token_price(self) -> Optional[int]:
        """
        Get the current collateral token price in USDT terms.

        The price is used to calculate the USD value of collateral:
        collateral_value_usdt = collateral_amount * price / PRICE_DECIMALS

        Returns:
            Price (Balance/u64) or None if error
        """
        try:
            result = self.contract.read(
                keypair=self.wallet.hotkey,
                method="get_collateral_token_price_for_testing",
            )

            data = result.contract_result_data.value_object
            if data and data[0] == "Ok":
                return data[1].value
            return None
        except Exception as e:
            bt.logging.error(f"Error getting collateral token price: {e}")
            return None


class TensorUSDAuctionContract:
    """
    Interface for interacting with the TensorUSD auction contract.

    Provides methods for:
    - Reading auction information
    - Placing bids on liquidation auctions
    - Fetching active auctions
    """

    def __init__(
        self,
        substrate: SubstrateInterface,
        contract_address: str,
        metadata_path: str,
        wallet: bt.Wallet,
    ):
        """
        Initialize auction contract interface.

        Args:
            substrate: Shared SubstrateInterface instance
            contract_address: SS58 address of the auction contract
            metadata_path: Path to tusdt_auction.json metadata file
            wallet: Wallet for signing transactions and querying
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

        bt.logging.info(f"TensorUSD auction contract initialized at {contract_address}")

    def place_bid(
        self,
        auction_id: int,
        bid_amount: int,
        keypair: Keypair,
        hotkey_ss58: str,
    ) -> Optional[str]:
        """
        Place a bid on a liquidation auction.

        Args:
            auction_id: Auction ID to bid on (u32)
            bid_amount: Bid amount in token units (Balance)
            keypair: Keypair to sign the transaction

        Returns:
            Transaction hash if successful, None otherwise
        """
        try:
            args = {
                "auction_id": auction_id,
                "bid_amount": bid_amount,
                "metadata": {"hot_key": hotkey_ss58},
            }

            gas_predict_result = self.contract.read(
                keypair=keypair,
                method="place_bid",
                args=args,
            )

            receipt = self.contract.exec(
                keypair=keypair,
                method="place_bid",
                args=args,
                gas_limit=gas_predict_result.gas_required,
            )

            if receipt.is_success:
                bt.logging.info(
                    f"Bid placed: auction={auction_id}, amount={bid_amount}, "
                    f"tx={receipt.extrinsic_hash}"
                )
                return receipt.extrinsic_hash
            else:
                bt.logging.error(f"Bid failed: {receipt.error_message}")
                return None

        except Exception as e:
            bt.logging.error(f"Error placing bid: {e}")
            return None

    def get_auction(self, auction_id: int) -> Optional[Auction]:
        """
        Get auction details by ID.

        Args:
            auction_id: Auction ID (u32)

        Returns:
            Auction dataclass or None if not found
        """
        try:
            result = self.contract.read(
                keypair=self.wallet.hotkey,
                method="get_auction",
                args={"auction_id": auction_id},
            )

            data = result.contract_result_data.value_object
            if data and data[0] == "Ok" and data[1]:
                auction_data = data[1].value
                return Auction(
                    id=auction_data["id"],
                    vault_owner=auction_data["vault_owner"],
                    vault_id=auction_data["vault_id"],
                    collateral_balance=auction_data["collateral_balance"],
                    debt_balance=auction_data["debt_balance"],
                    starts_at=auction_data["starts_at"],
                    ends_at=auction_data["ends_at"],
                    highest_bidder=auction_data.get("highest_bidder"),
                    highest_bid=auction_data["highest_bid"],
                    highest_bid_id=auction_data.get("highest_bid_id"),
                    bid_count=auction_data["bid_count"],
                    is_finalized=auction_data["is_finalized"],
                )
            return None
        except Exception as e:
            bt.logging.error(f"Error getting auction: {e}")
            return None

    def get_auction_bid(self, auction_id: int, bidder: str) -> Optional[Bid]:
        """
        Get auction bid by auction_id and bidder.
        """
        try:
            result = self.contract.read(
                keypair=self.wallet.hotkey,
                method="get_auction_bid",
                args={"auction_id": auction_id, "bidder": bidder},
            )
            data = result.contract_result_data.value_object
            if data and data[0] == "Ok" and data[1]:
                if data[1].value is None:
                    return None
                else:
                    return Bid(
                        id=data[1].value["id"],
                        auction_id=data[1].value["auction_id"],
                        bidder=data[1].value["bidder"],
                        amount=data[1].value["amount"],
                    )
            return None

        except Exception as e:
            bt.logging.error(f"Error getting auction bid: {e}")
            return None

    def get_active_auctions_count(self) -> int:
        """Get count of active auctions."""
        try:
            result = self.contract.read(
                keypair=self.wallet.hotkey,
                method="get_active_auctions_count",
            )
            data = result.contract_result_data.value_object
            if data and data[0] == "Ok":
                return data[1].value
            return 0
        except Exception as e:
            bt.logging.error(f"Error getting active auctions count: {e}")
            return 0

    def get_current_block(self) -> int:
        return self.substrate.get_block_number(None)

    def get_current_timestamp(self) -> int:
        """
        Get current blockchain timestamp in milliseconds.

        Returns:
            Current timestamp from the Timestamp pallet
        """
        try:
            result = self.substrate.query("Timestamp", "Now")
            return result.value
        except Exception as e:
            bt.logging.error(f"Error getting blockchain timestamp: {e}")
            return 0

    def get_active_auctions(self) -> List[ActiveAuction]:
        """
        Get all active auctions from the contract.

        First fetches count, then retrieves paginated results (10 per page).
        Filters auctions to only return those where ends_at > current blockchain timestamp.

        Returns:
            List of ActiveAuction objects for auctions still in progress
        """
        active_auctions: List[ActiveAuction] = []
        PAGE_SIZE = 10

        bt.logging.info("Fetching active auctions from contract...")

        try:
            current_timestamp = self.get_current_timestamp()
            if current_timestamp == 0:
                bt.logging.warning(
                    "Could not get blockchain timestamp, using all auctions"
                )

            total_count = self.get_active_auctions_count()
            if total_count == 0:
                bt.logging.info("No active auctions found")
                return []

            total_pages = (total_count + PAGE_SIZE - 1) // PAGE_SIZE
            bt.logging.info(
                f"Found {total_count} active auctions across {total_pages} pages"
            )

            for page in range(total_pages):
                result = self.contract.read(
                    keypair=self.wallet.hotkey,
                    method="get_active_auctions",
                    args={"page": page},
                )

                data = result.contract_result_data.value_object
                if data and data[0] == "Ok" and data[1]:
                    auctions_list = data[1].value["Ok"]

                    for auction_data in auctions_list:
                        ends_at = auction_data["ends_at"]

                        # Filter: only include auctions that haven't ended yet
                        if current_timestamp > 0 and ends_at <= current_timestamp:
                            bt.logging.debug(
                                f"Skipping expired auction {auction_data['id']}: "
                                f"ends_at={ends_at} <= current={current_timestamp}"
                            )
                            continue

                        active_auctions.append(
                            ActiveAuction(
                                auction_id=auction_data["id"],
                                vault_owner=auction_data["vault_owner"],
                                vault_id=auction_data["vault_id"],
                                collateral_balance=auction_data["collateral_balance"],
                                debt_balance=auction_data["debt_balance"],
                                highest_bid=auction_data["highest_bid"],
                                highest_bidder=auction_data.get("highest_bidder"),
                                ends_at=ends_at,
                            )
                        )

            bt.logging.info(
                f"Fetched {len(active_auctions)} active auctions "
                f"(filtered by ends_at > {current_timestamp})"
            )
            return active_auctions

        except Exception as e:
            bt.logging.error(f"Error fetching active auctions: {e}")
            return []
