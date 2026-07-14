# TensorUSD Subnet (SN113)

> **Decentralized liquidation auctions and price oracle for TensorUSD stablecoin on Bittensor**

Miners earn TAO by participating in liquidation auctions, contributing to the price oracle, and submitting prediction agents. Validators track on-chain activity and distribute rewards.

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

- **Mechanism 0**: Liquidation auctions and prediction agent (bid on undercollateralized vaults)
- **Mechanism 1**: Prediction Agent (Predict vault health and minted tokens after exactly one week)

### Option 1: Liquidation (Mechanism 0)
> [!NOTE]
> - $${\color{yellow}Staking: Miners \space must \space have \space at \space least \space 10 \space alpha \space staked \space to \space their \space own \space hotkey \space to \space participate \space in \space this \space mechanism.}$$
> - $${\color{yellow}Fees: Running \space this \space mechanism \space requires \space submitting \space extrinsics \space to \space the \space chain, \space which \space will \space incur \space standard \space TAO \space transaction \space costs.}$$
> - $${\color{yellow}Security: Ensure \space your \space coldkey \space is \space kept \space entirely \space safe \space and \space secure.}$$

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

### Option 2: Prediction Agent (Mechanism 1)
> [!NOTE]
> * $${\color{yellow}Initial \space TAO \space Deposit: You \space will \space need \space to \space send \space some \space TAO \space initially \space to \space cover \space your \space LLM \space token \space costs, \space calculated \space based \space on \space your \space input \space and \space output \space tokens.}$$


Miners can also submit a **prediction agent** - a Python script that generates CSV predictions for vault health and number of tokens minted in a week time. The best agent winner takes all Mechanism 1 reward:

```bash
uv run tensorusd.py submit --agent <agent_location>
```
Agent submission console will ask you to provide number of expected input and output tokens necessary to run agent, select a model of choice and pay the evaluation cost.


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

# only agent env
TENSORUSD_SN_BACKEND_URL=https://agent-api.tensorusd.com
TENSORUSD_NETUID=113

OPENAI_API_KEY=your_openai_api_key # Fot agent validator only

```

Then run:

```bash
uv run neurons/miner/liquidator.py --netuid 113 \
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
---

## 🔍 Running a Validator

Validators monitor on-chain events, evaluate prediction agents, and distribute rewards for both mechanisms.

### Validator Mechanism 0: Liquidation

#### Liquidation

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

#### Validator Mechanism 1: Prediction Agent

Before running Agent evaluation; validators should first create docker image for sandobox environment using:
```bash
docker build -f Dockerfile.sandbox -t tensorusd-sandbox:latest .
```
*[NOTE: Keep the name of docker image as it is, because it is referenced in the codebase.]*

Then, run the validator
```bash
uv run neurons/validator/agent.py \
  --mechid 1 \
  --subtensor.network finney \
  --wallet.name validator_wallet \
  --wallet.hotkey validator_hotkey \
  --logging.info \
```

## 🎯 How It Works

### Mechanism 0: Liquidation Auctions

**Miners:**

1. Monitor auction contract for new liquidation events
2. Calculate profitability: `profit = collateral_value - bid - debt`
3. Submit competitive bids if profit margin meets threshold
4. Win auctions by having highest bid when auction ends

### Mechanism 1: Prediction Agents

**Prediction Agent:**

Miners submit Python agents that predict vault health metrics and number of minted tokens in a week's time (meaning, the agent submitted this sunday will be evaluated on actual data of next sunday). Agents are:
1. Downloaded and validated (security, format, plagiarism checks)
2. Executed in a sandboxed environment with LLM agents(only openai is allowed for now)
3. Output CSV predictions stored for delayed scoring

The winner agent receives full reward of mechanism 1.

**Validators:**

1. Listen to `AuctionFinalized` events
2. Extract winner hotkey from bid metadata
3. Calculate rewards (1.0 base + up to 1.0 bonus for overbidding)
4. Optionally add agent bonus (+20% of total rewards) to the best agent winner
5. Set weights with `mechid=0`

**Reward Formula:**

```python
# For liquidator 
BASE_REWARD = 1.0
BONUS_THRESHOLD = 0.20  # 20% overpay for max bonus
bonus_ratio = min((winning_bid - debt_balance) / debt_balance, BONUS_THRESHOLD)
reward = bonus_ratio + BASE_REWARD

# For Agent evalution:
REWARD = 1.0 for the best agent
```

### Mechanism 1: Prediction Agent

**Miners:**

1. Get the info of liquidators.
2. Predict vault health of liquidators

**Validators:**

1. Keep track of uploaded agents
2. Run the agents in sandbox environment.
2. Fetch vault information.
3. Compare agent prediction to the collected vault information.
4. Reward the best miner.
