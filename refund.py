"""
Refund checker script for miners.

Checks whether the miner got all their bidded amount refunded
after an auction is finalized, and attempts to withdraw any unclaimed refunds.

Usage:
    python refund.py
"""

import os
from substrateinterface import SubstrateInterface, Keypair
from substrateinterface.contracts import ContractInstance

# ──────────────────────────────────────────────────────────────
# Configuration — update these values before running
# ──────────────────────────────────────────────────────────────
RPC_ENDPOINT = "wss://test.finney.opentensor.ai:443"
CONTRACT_ADDRESS = "5Gg9M2BBG4goA9upeo9CeNLreZsp99yooLQnfJ66DHUh6ukq"
METADATA_PATH = os.path.join(os.path.dirname(__file__), "tensorusd/abis/tusdt_auction.json")

# Miner's mnemonic (coldkey) — used to sign withdraw transactions
MINER_MNEMONIC = "your mnemonic here"

# Auction ID to check (set to None to check all finalized auctions the miner participated in)
AUCTION_ID = None

PAGE_SIZE = 10
# ──────────────────────────────────────────────────────────────


def connect():
    substrate = SubstrateInterface(
        url=RPC_ENDPOINT,
        use_remote_preset=True,
        type_registry={"types": {"Balance": "u64"}},
    )
    contract = ContractInstance.create_from_address(
        contract_address=CONTRACT_ADDRESS,
        metadata_file=METADATA_PATH,
        substrate=substrate,
    )
    keypair = Keypair.create_from_mnemonic(MINER_MNEMONIC)
    return substrate, contract, keypair


def get_auction(contract, keypair, auction_id):
    result = contract.read(keypair, "get_auction", {"auction_id": auction_id})
    data = result.contract_result_data.value_object
    if data and data[0] == "Ok" and data[1]:
        return data[1].value
    return None


def get_total_auctions_count(contract, keypair):
    result = contract.read(keypair, "get_total_auctions_count")
    data = result.contract_result_data.value_object
    if data and data[0] == "Ok":
        return data[1].value
    return 0


def get_bids_page(contract, keypair, auction_id, page):
    result = contract.read(
        keypair, "get_bids",
        {"auction_id": auction_id, "page": page},
    )
    data = result.contract_result_data.value_object
    if data and data[0] == "Ok" and data[1]:
        return data[1].value["Ok"]
    return []


def withdraw_refund(contract, keypair, auction_id, bid_id):
    args = {"auction_id": auction_id, "bid_id": bid_id}
    gas_result = contract.read(keypair, "withdraw_refund", args)
    receipt = contract.exec(
        keypair, "withdraw_refund", args,
        gas_limit=gas_result.gas_required,
    )
    return receipt


def check_auction(contract, keypair, auction_id, auto_withdraw=False):
    """Check a single auction for unrefunded bids. Returns summary dict."""
    my_address = keypair.ss58_address
    auction = get_auction(contract, keypair, auction_id)

    if auction is None:
        print(f"  Auction {auction_id}: not found, skipping")
        return None

    is_finalized = auction["is_finalized"]
    highest_bid_id = auction.get("highest_bid_id")
    highest_bidder = auction.get("highest_bidder")
    bid_count = auction["bid_count"]
    is_winner = highest_bidder == my_address

    # Collect all of the miner's bids for this auction
    my_bids = []
    total_pages = (bid_count + PAGE_SIZE - 1) // PAGE_SIZE if bid_count > 0 else 0

    for page in range(total_pages):
        bids = get_bids_page(contract, keypair, auction_id, page)
        for bid in bids:
            if bid["bidder"] == my_address:
                my_bids.append(bid)

    if not my_bids:
        return None  # miner didn't participate

    # Categorize bids
    winning_bid = None
    refundable_bids = []
    withdrawn_bids = []
    pending_bids = []

    for bid in my_bids:
        bid_id = bid["id"]
        amount = bid["amount"]
        is_withdrawn = bid["is_withdrawn"]

        # The winning bid is not refundable
        if is_winner and bid_id == highest_bid_id:
            winning_bid = bid
            continue

        if is_withdrawn:
            withdrawn_bids.append(bid)
        else:
            pending_bids.append(bid)

    # Print report
    total_bidded = sum(b["amount"] for b in my_bids)
    total_withdrawn = sum(b["amount"] for b in withdrawn_bids)
    total_pending = sum(b["amount"] for b in pending_bids)
    winning_amount = winning_bid["amount"] if winning_bid else 0

    status = "FINALIZED" if is_finalized else "ACTIVE"
    print(f"\n  Auction {auction_id} [{status}]")
    print(f"    You placed {len(my_bids)} bid(s), total amount: {total_bidded}")
    if is_winner:
        print(f"    ** You WON this auction (winning bid #{winning_bid['id']}, amount: {winning_amount}) **")
    print(f"    Refunds withdrawn:  {len(withdrawn_bids)} bid(s), amount: {total_withdrawn}")
    print(f"    Refunds pending:    {len(pending_bids)} bid(s), amount: {total_pending}")

    # Attempt auto-withdraw if requested
    if auto_withdraw and is_finalized and pending_bids:
        print(f"    Attempting to withdraw {len(pending_bids)} pending refund(s)...")
        for bid in pending_bids:
            bid_id = bid["id"]
            amount = bid["amount"]
            try:
                receipt = withdraw_refund(contract, keypair, auction_id, bid_id)
                if receipt.is_success:
                    print(f"      ✓ Withdrawn bid #{bid_id}, amount: {amount}, tx: {receipt.extrinsic_hash}")
                else:
                    print(f"      ✗ Failed bid #{bid_id}: {receipt.error_message}")
            except Exception as e:
                print(f"      ✗ Error withdrawing bid #{bid_id}: {e}")

    return {
        "auction_id": auction_id,
        "is_finalized": is_finalized,
        "is_winner": is_winner,
        "total_bids": len(my_bids),
        "total_bidded": total_bidded,
        "winning_amount": winning_amount,
        "withdrawn": total_withdrawn,
        "pending": total_pending,
        "pending_count": len(pending_bids),
    }


def main():
    print("Connecting to chain...")
    substrate, contract, keypair = connect()
    print(f"Miner address: {keypair.ss58_address}")
    print(f"Contract: {CONTRACT_ADDRESS}")

    # Ask user whether to auto-withdraw
    auto_withdraw = input("\nAuto-withdraw pending refunds? (y/n): ").strip().lower() == "y"

    auction_ids = []
    if AUCTION_ID is not None:
        auction_ids = [AUCTION_ID]
    else:
        total = get_total_auctions_count(contract, keypair)
        print(f"Total auctions on contract: {total}")
        auction_ids = range(total)

    results = []
    for aid in auction_ids:
        try:
            summary = check_auction(contract, keypair, aid, auto_withdraw)
            if summary:
                results.append(summary)
        except Exception as e:
            print(f"  Error checking auction {aid}: {e}")

    # Final summary
    if not results:
        print("\nNo auctions found where this miner participated.")
        return

    total_pending = sum(r["pending"] for r in results)
    total_pending_count = sum(r["pending_count"] for r in results)
    total_withdrawn = sum(r["withdrawn"] for r in results)
    total_won = sum(r["winning_amount"] for r in results)

    print("\n" + "=" * 55)
    print("REFUND SUMMARY")
    print("=" * 55)
    print(f"  Auctions participated in:  {len(results)}")
    print(f"  Total refunds withdrawn:   {total_withdrawn}")
    print(f"  Total refunds pending:     {total_pending} ({total_pending_count} bid(s))")
    print(f"  Total spent on wins:       {total_won}")

    if total_pending > 0:
        print(f"\n  ⚠ You have {total_pending} TUSDT in unclaimed refunds!")
        if not auto_withdraw:
            print("  Run again with auto-withdraw to claim them.")
    else:
        print("\n  ✓ All refunds accounted for. Nothing left behind.")


if __name__ == "__main__":
    main()