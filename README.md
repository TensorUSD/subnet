# TensorUSD Subnet (SN113)

> **Decentralized liquidation auctions and price oracle for TensorUSD stablecoin on Bittensor**

Miners earn TAO by participating in liquidation auctions and contributing to the price oracle. Validators track on-chain activity and distribute rewards.

📚 **[Documentation](https://docs.tensorusd.com/components/subnet)**

---

## 🚀 Quick Start

### Prerequisites

1. **Python 3.10+** installed
2. **Bittensor wallet** registered on netuid 113
3. **[uv](https://docs.astral.sh/uv/)**
4. **For Miners**: TUSDT tokens + CoinMarketCap API key

### Installation

```bash
# Clone repository
git clone https://github.com/TensorUSD/subnet
cd subnet

# Install dependencies
uv sync

# Install as package
uv pip install -e .

# Run database migrations (validators only)
uv run alembic upgrade head
```

---

## ⚡ Running a Miner

Miners can participate in **two mechanisms** to earn rewards:

- **Mechanism 0**: Liquidation auctions (bid on undercollateralized vaults)
- **Mechanism 1**: Price oracle (submit TAO/USD prices)

### Option 1: Run Both Mechanisms (Recommended)

```bash
uv run neurons/miner.py \
  --netuid 113 \
  --subtensor.network finney \
  --wallet.name my_wallet \
  --wallet.hotkey my_hotkey \
  --mech.ids 0,1 \
  --auction_contract.address 5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty \
  --vault_contract.address 5CiPPseXPECbkjWca6MnjNokrgYjMqmKndv2rSnekmSK2DjL \
  --tusdt.address 5DAAnrj7VKbSBAiC3R9YJY4g8eZN8DLqr3gZJvJT8qYgL3Nq \
  --oracle_contract.address 5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY \
  --cmc.api_key YOUR_COINMARKETCAP_API_KEY \
  --price.submission_interval_seconds 300 \
  --coldkey.password YOUR_COLDKEY_PASSWORD
```

### Option 2: Liquidation Only (Mechanism 0)

```bash
uv run neurons/miner.py \
  --netuid 113 \
  --subtensor.network finney \
  --wallet.name my_wallet \
  --wallet.hotkey my_hotkey \
  --mech.ids 0 \
  --auction_contract.address 5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty \
  --vault_contract.address 5CiPPseXPECbkjWca6MnjNokrgYjMqmKndv2rSnekmSK2DjL \
  --tusdt.address 5DAAnrj7VKbSBAiC3R9YJY4g8eZN8DLqr3gZJvJT8qYgL3Nq \
  --coldkey.password YOUR_COLDKEY_PASSWORD
```

### Option 3: Price Oracle Only (Mechanism 1)

```bash
uv run neurons/miner.py \
  --netuid 113 \
  --subtensor.network finney \
  --wallet.name my_wallet \
  --wallet.hotkey my_hotkey \
  --mech.ids 1 \
  --oracle_contract.address 5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY \
  --cmc.api_key YOUR_COINMARKETCAP_API_KEY \
  --price.submission_interval_seconds 300
```

### Using Environment Variables

Create a `.env` file to avoid passing secrets via CLI:

```bash
# Required for all miners
WALLET_NAME=my_wallet
WALLET_HOTKEY=my_hotkey

# Mechanism 0: Liquidation auctions
AUCTION_CONTRACT_ADDRESS=5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty
VAULT_CONTRACT_ADDRESS=5CiPPseXPECbkjWca6MnjNokrgYjMqmKndv2rSnekmSK2DjL
TOKEN_CONTRACT_ADDRESS=5DAAnrj7VKbSBAiC3R9YJY4g8eZN8DLqr3gZJvJT8qYgL3Nq
COLDKEY_PASSWORD=your_secure_password

# Mechanism 1: Price oracle
ORACLE_CONTRACT_ADDRESS=5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY
CMC_API_KEY=your_coinmarketcap_api_key
PRICE_SUBMISSION_INTERVAL=300

# Mechanism selection (comma-separated)
MECH_IDS=0,1
```

Then run:

```bash
uv run neurons/miner.py --netuid 113 \
--subtensor network <finney | test> \
--wallet.name miner \
--wallet.hotkey default \
--logging.info
```

### Miner Configuration Options

#### Mechanism 0: Liquidation Bidding Strategy

| Option                     | Default | Description                                                |
| -------------------------- | ------- | ---------------------------------------------------------- |
| `--bid.initial_percentage` | 0.0005  | Initial bid as % above debt (e.g., 0.0005 = debt × 1.0005) |
| `--bid.increment_rate`     | 0.0005  | Increase bid by % when outbid (e.g., 0.005 = +0.5%)        |
| `--bid.max_percentage`     | 0.95    | Maximum bid as % of collateral value (safety limit)        |
| `--bid.min_profit_margin`  | 0.0002  | Minimum profit margin to place bid (e.g., 0.02%)           |

**Example: Aggressive bidding strategy**

```bash
uv run neurons/miner.py \
  --mech.ids 0 \
  --bid.initial_percentage 0.001 \
  --bid.increment_rate 0.002 \
  --bid.max_percentage 0.90 \
  --bid.min_profit_margin 0.0001 \
  ... # other required args
```

#### Mechanism 1: Price Oracle Configuration

| Option                                | Default | Description                                           |
| ------------------------------------- | ------- | ----------------------------------------------------- |
| `--oracle_contract.address`           | env     | Oracle contract SS58 address                          |
| `--cmc.api_key`                       | env     | CoinMarketCap API key                                 |
| `--price.submission_interval_seconds` | env     | Seconds between price submissions (e.g., 300 = 5 min) |

---

## 🔍 Running a Validator

Validators monitor on-chain events and distribute rewards for both mechanisms.

### Basic Validator

```bash
uv run neurons/validator.py \
  --netuid 113 \
  --subtensor.network finney \
  --wallet.name validator_wallet \
  --wallet.hotkey validator_hotkey \
  --logging.info \
  --auction_contract.address 5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty \
  --oracle_contract.address 5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY \
```

## 🎯 How It Works

### Mechanism 0: Liquidation Auctions

**Miners:**

1. Monitor auction contract for new liquidation events
2. Calculate profitability: `profit = collateral_value - bid - debt`
3. Submit competitive bids if profit margin meets threshold
4. Win auctions by having highest bid when auction ends

**Validators:**

1. Listen to `AuctionFinalized` events
2. Extract winner hotkey from bid metadata
3. Calculate rewards (1.0 base + up to 1.0 bonus for overbidding)
4. Set weights with `mechid=0`

**Reward Formula:**

```python
BASE_REWARD = 1.0
BONUS_THRESHOLD = 0.20  # 20% overpay for max bonus
bonus_ratio = min((winning_bid - debt_balance) /debt_balance, BONUS_THRESHOLD)
reward = bonus_ratio + BASE_REWARD
return reward
```

### Mechanism 1: Price Oracle

**Miners:**

1. Fetch TAO/USD price from CoinMarketCap API every 5 minutes
2. Convert to u128 ratio: `price_ratio = price_usd * 10^18`
3. Submit to oracle contract with hotkey metadata
4. Participate in consensus rounds

**Validators:**

1. Query oracle for completed rounds
2. Fetch all price submissions via `get_round_submissions(round_id)`
3. Compare submissions to price
4. Reward accuracy (submissions close to price get higher scores)
5. Set weights with `mechid=1`

**Reward Criteria:**

- High accuracy (within 0.1% of median): 1.0 reward
- Good accuracy (within 1% of median): 0.85 reward
- Poor accuracy (>5% deviation): 0.0 reward
- Non-participation: 0.0 reward
