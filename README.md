# TensorUSD Subnet (SN113)

> **Decentralized liquidation auctions and price oracle for TensorUSD stablecoin on Bittensor**

Miners earn TAO by participating in liquidation auctionsand contributing to the price oracle. Validators track on-chain activity and distribute rewards.

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

## 📋 Contract Addresses (Mainnet)

| Contract      | Address                                            |
| ------------- | -------------------------------------------------- |
| Vault         | `5F8ykW4bse6kUHi65XqAzSfrrgKHDXXEBoReUZmUVc7r8q3A` |
| Auction       | `5Djyz3DAsL6HyZGBFKNK7fdaMP2Q21hn5sdPhigpHdcfGZ1a` |
| Token (TUSDT) | `5GjL2MKErF9ocXZBZZFueoWgf8wAnY1gcgLkDMj2bTsAsg6g` |
| Oracle        | `5GcaftCj1psi5489Dp8RiL5UmMsbRMf9XsfNrDMMsfM5hFoB` |

---

## ⚡ Running a Miner

Miners can participate in **two mechanisms** to earn rewards:

- **Mechanism 0**: Liquidation auctions (bid on undercollateralized vaults)
- **Mechanism 1**: Price oracle (submit TAO/USD prices)

### Option 1: Liquidation Only (Mechanism 0)

```bash
uv run neurons/miner/liquidator.py \
  --netuid 113 \
  --subtensor.network finney \
  --wallet.name my_wallet \
  --wallet.hotkey my_hotkey \
  --auction_contract.address 5Djyz3DAsL6HyZGBFKNK7fdaMP2Q21hn5sdPhigpHdcfGZ1a \
  --vault_contract.address 5F8ykW4bse6kUHi65XqAzSfrrgKHDXXEBoReUZmUVc7r8q3A \
  --tusdt.address 5GjL2MKErF9ocXZBZZFueoWgf8wAnY1gcgLkDMj2bTsAsg6g \
  --coldkey.password YOUR_COLDKEY_PASSWORD
```

### Option 2: Price Oracle Only (Mechanism 1)

```diff
! Miner should have at least 10 alpha staked to their own hotkey to participate in this mechanism.
```

```bash
uv run neurons/miner/oracle.py \
  --netuid 113 \
  --subtensor.network finney \
  --wallet.name my_wallet \
  --wallet.hotkey my_hotkey \
  --mech.ids 1 \
  --oracle_contract.address 5GcaftCj1psi5489Dp8RiL5UmMsbRMf9XsfNrDMMsfM5hFoB \
  --cmc.api_key YOUR_COINMARKETCAP_API_KEY \
  --price.submission_interval_seconds 1800 \
  --price.monitor_interval_seconds 300 \
  --price.change_threshold 0.017 \
  --price.provider coinmarketcap
```

### Using Environment Variables

Create a `.env` file to avoid passing secrets via CLI:

```bash
# Miner and validator related env
VAULT_CONTRACT_ADDRESS=5F8ykW4bse6kUHi65XqAzSfrrgKHDXXEBoReUZmUVc7r8q3A
AUCTION_CONTRACT_ADDRESS=5Djyz3DAsL6HyZGBFKNK7fdaMP2Q21hn5sdPhigpHdcfGZ1a
TOKEN_CONTRACT_ADDRESS=5GjL2MKErF9ocXZBZZFueoWgf8wAnY1gcgLkDMj2bTsAsg6g
ORACLE_CONTRACT_ADDRESS=5GcaftCj1psi5489Dp8RiL5UmMsbRMf9XsfNrDMMsfM5hFoB

# only miner related env
COLDKEY_PASSWORD=your_coldkey_password
CMC_API_KEY=your_coinmarketcap_api_key
PRICE_SUBMISSION_INTERVAL=21600 # 6 hours
PRICE_MONITOR_INTERVAL=60 # 1 min
PRICE_CHANGE_THRESHOLD=0.017 # 1.7%
PRICE_PROVIDER=coinmarketcap

# only validator env
DATABASE_URL=sqlite:///tensorusd.db


## AGENT ENVs (ignore for now)
TENSORUSD_SN_BACKEND_URL=
TENSORUSD_NETUID=113
BITTENSOR_NETWORK=

```

Then run:

```bash
uv run neurons/miner/liquidator.py --netuid 113 \
--subtensor network <finney | test> \
--wallet.name miner \
--wallet.hotkey default \
--logging.info

uv run neurons/miner/oracle.py --netuid 113 \
--subtensor network <finney | test> \
--wallet.name miner \
--wallet.hotkey default \
--logging.info
```

### Miner Configuration Options

#### Mechanism 0: Liquidation Bidding Strategy

| Option                     | Default | Description                                                                                                             |
| -------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------- |
| `--bid.initial_percentage` | 0.11    | Initial bid as % above debt (e.g., 0.11 = debt × 1.11) (Remember contract accepts auction with at least 11% above debt) |
| `--bid.increment_rate`     | 0.0005  | Increase bid by % when outbid (e.g., 0.005 = +0.5%)                                                                     |
| `--bid.max_percentage`     | 0.3     | Maximum bid as % of collateral value (safety limit)                                                                     |
| `--bid.min_profit_margin`  | 0.0002  | Minimum profit margin to place bid (e.g., 0.02%)                                                                        |

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

| Option                                | Default | Description                                                                   |
| ------------------------------------- | ------- | ----------------------------------------------------------------------------- |
| `--oracle_contract.address`           | env     | Oracle contract SS58 address                                                  |
| `--cmc.api_key`                       | env     | CoinMarketCap API key                                                         |
| `--price.submission_interval_seconds` | env     | Seconds between price submissions (e.g., 1800 = 30 mins)                      |
| `--price.monitor_interval_seconds`    | env     | Interval in seconds between price monitors from the api. (e.g., 300 = 5 mins) |
| `--price.change_threshold`            | env     | Change threshold for force price submission (e.g., 0.017 = 1.7 %)             |
| `--price.provider`                    | env     | Price provider (e.g., coinmarketcap)                                          |

---

## 🔍 Running a Validator

Validators monitor on-chain events and distribute rewards for both mechanisms.

### Validator Mechanism 0: Liquidation

```bash
uv run neurons/validator/liquidator.py \
  --netuid 113 \
  --subtensor.network finney \
  --wallet.name validator_wallet \
  --wallet.hotkey validator_hotkey \
  --logging.info \
  --auction_contract.address 5Djyz3DAsL6HyZGBFKNK7fdaMP2Q21hn5sdPhigpHdcfGZ1a \
  --oracle_contract.address 5GcaftCj1psi5489Dp8RiL5UmMsbRMf9XsfNrDMMsfM5hFoB \
```

### Validator Mechanism 1: Oracle

```bash
uv run neurons/validator/oracle.py \
  --netuid 113 \
  --subtensor.network finney \
  --wallet.name validator_wallet \
  --wallet.hotkey validator_hotkey \
  --logging.info \
  --oracle_contract.address 5GcaftCj1psi5489Dp8RiL5UmMsbRMf9XsfNrDMMsfM5hFoB \
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
