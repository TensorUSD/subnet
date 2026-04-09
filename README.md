# TensorUSD Subnet

Bittensor subnet for TensorUSD liquidation auctions. Miners compete to win liquidation auctions by placing bids, and validators track auction outcomes to reward winning miners.

### Contracts

The subnet interacts with three ink! smart contracts on Substrate:

- **Vault Contract** - Manages collateralized vaults, provides collateral token price
- **Auction Contract** - Handles liquidation auctions (create, bid, finalize)
- **TUSDT Contract** - ERC20 token used for bidding

## Development Setup

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Installation

```bash
# Clone the repository
git clone https://github.com/TensorUSD/subnet
cd subnet

# Install dependencies with uv
uv sync

# install as a package
uv pip install -e .
```

### Environment Variables

Create a `.env` file:

```bash
# can also be passed as CLI args

COLDKEY_PASSWORD=your_coldkey_password # Required For Miner Only
AUCTION_CONTRACT_ADDRESS=5xxx... # Required For Both
VAULT_CONTRACT_ADDRESS=5xxx... # Required For Miner Only
TOKEN_CONTRACT_ADDRESS=5xxx... # Required For Miner Only

```

### Database Setup (Validator)

```bash
# Apply migrations
uv run alembic upgrade head
```

## Running

### Miner

```bash
# Basic
uv run neurons/miner.py \
    --netuid <NETUID> \
    --subtensor.network finney \
    --wallet.name <WALLET_NAME> \
    --wallet.hotkey <HOTKEY_NAME> \
    --auction_contract.address <AUCTION_ADDRESS> \
    --vault_contract.address <VAULT_ADDRESS> \
    --tusdt.address <TUSDT_ADDRESS> \
    --coldkey.password <COLDKEY_PASSWORD>

# With custom bidding strategy
uv run neurons/miner.py \
    --netuid <NETUID> \
    --subtensor.network finney \
    --wallet.name <WALLET_NAME> \
    --wallet.hotkey <HOTKEY_NAME> \
    --auction_contract.address <AUCTION_ADDRESS> \
    --vault_contract.address <VAULT_ADDRESS> \
    --tusdt.address <TUSDT_ADDRESS> \
    --coldkey.password <COLDKEY_PASSWORD> \
    --bid.initial_percentage 0.001 \
    --bid.increment_rate 0.001 \
    --bid.max_percentage 0.90 \
    --bid.min_profit_margin 0.0005
```

#### Miner CLI Options

| Option                       | Default | Description                                 |
| ---------------------------- | ------- | ------------------------------------------- |
| `--auction_contract.address` | env     | Auction contract SS58 address               |
| `--vault_contract.address`   | env     | Vault contract SS58 address                 |
| `--tusdt.address`            | env     | TUSDT token contract SS58 address           |
| `--tusdt.approval_amount`    | 0 (max) | Amount to approve for auction contract      |
| `--wallet.password`          | None    | Wallet password required for coldkey unlock |
| `--bid.initial_percentage`   | 0.0005  | Initial bid as % of collateral value        |
| `--bid.increment_rate`       | 0.0005  | Bid increment when outbid                   |
| `--bid.max_percentage`       | 0.95    | Maximum bid as % of collateral value        |
| `--bid.min_profit_margin`    | 0.0002  | Minimum profit margin to bid                |

### Validator

```bash
uv run neurons/validator.py \
    --netuid <NETUID> \
    --subtensor.network finney \
    --wallet.name <WALLET_NAME> \
    --wallet.hotkey <HOTKEY_NAME> \
    --auction_contract.address <AUCTION_ADDRESS> \
```

#### Validator CLI Options

| Option                       | Default | Description                 |
| ---------------------------- | ------- | --------------------------- |
| `--auction_contract.address` | env     | Vault contract SS58 address |

## How It Works

### Miner Flow

See [Miner Guide](docs/miner.md) for details on miner behavior, incentives, and runtime options.

1. **Event Listener** subscribes to auction contract events
2. On `AuctionCreated`:
   - Fetch auction data from chain
   - Get collateral price from vault contract
   - Calculate profitable bid using strategy
   - Submit bid if profitable
3. On `BidPlaced` (by others):
   - Fetch current auction state from chain
   - Skip if already highest bidder
   - Calculate counter-bid if profitable
   - Submit counter-bid
4. On `AuctionFinalized`:
   - Log win/loss result

### Validator Flow

See [Validator Guide](docs/validator.md) for details on reward calculation, weight setting, and validator runtime flow.

1. **Event Listener** subscribes to auction contract events
2. Store all events in SQLite database
3. Track auction wins with winner hotkey
4. Calculate rewards based on auction wins per tempo

### Profit Calculation

```
collateral_value = collateral * collateral_price / 10^12
profit = collateral_value - bid_amount - debt_balance
profit_margin = profit / collateral_value
```

Miner only bids if `profit_margin >= min_profit_margin`.
