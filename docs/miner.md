# Miner

## What Miners Do

Miners participate in liquidation auctions on the TensorUSD protocol. A vault is liquidated when its collateral value drops below **1.2× the debt** (liquidation ratio). At that point the vault is put up for auction. Miners bid TUSDT to win the vault's collateral. Because the collateral is worth at least 1.2× the debt at liquidation, a miner who bids close to the debt amount captures the spread as profit.

On top of that, winning auctions earns the miner **TAO emissions** from the Bittensor subnet.

---

## How Miners Earn

There are two sources of income:

### 1. On-chain Profit (from the auction itself)

The miner pays the winning bid in TUSDT and receives the vault's collateral tokens.

```
liquidation triggers when:
  collateral_value < liquidation_ratio × debt
  liquidation_ratio = 1.2

profit = collateral_value - winning_bid

where:
  collateral_value = collateral_amount × collateral_price  (in TUSDT)
  winning_bid      = amount paid by the miner (in TUSDT)
```

Because the liquidation ratio is 1.2, `collateral_value ≥ 1.2 × debt` is guaranteed at the point of liquidation. This creates a built-in spread of at least 20% above debt for miners to capture.

**Example:**
- Vault collateral: `1 TAO` at price `150 TUSDT` → `collateral_value = 150 TUSDT`
- Vault debt: `100 TUSDT`
- Liquidation ratio: `1.2` → vault goes to auction when collateral value falls below `120 TUSDT`
- Collateral value at auction: `120 TUSDT`  _(1.2 × 100)_
- Miner's winning bid: `110 TUSDT`
- On-chain profit: `120 - 110 = 10 TUSDT`

### 2. TAO Emissions (from Bittensor subnet rewards)

Every epoch, the validator reads all auction wins from that period and assigns weights to miners. Those weights determine each miner's share of the subnet's TAO emissions.

---

## Incentive Mechanism (Reward Formula)

The validator scores each miner's win using a linear reward function based on how much the winning bid exceeds the vault's debt:

```
bonus_ratio = min((winning_bid - debt_balance) / debt_balance, 0.20)

reward = 1.0 + bonus_ratio
```

| Winning Bid vs Debt | `bonus_ratio` | `reward` |
|---|---|---|
| Exactly debt (`bid = debt`) | `0.00` | `1.0` |
| 10% over debt | `0.10` | `1.1` |
| 20% over debt | `0.20` | `1.2` |
| 30% over debt (capped) | `0.20` | `1.2` |

- Minimum reward per win: **1.0**
- Maximum reward per win: **1.2**
- The bonus caps at **20% overpay** — bidding more than that gives no extra TAO reward.

**Multiple wins in the same epoch accumulate:**

```
total_reward(miner) = sum of reward(win_i) for all wins in tempo window
```

So a miner who wins three auctions in one epoch with rewards `1.0 + 1.5 + 2.0` gets a total score of `4.5` before normalisation.

### Weight Setting

At the end of each epoch the validator:

1. Collects all per-miner accumulated rewards.
2. Applies an exponential moving average (EMA) to smooth scores over time:

```
score = α × new_reward + (1 - α) × previous_score
```

3. L1-normalises all scores across miners and submits them as weights to the chain. Each miner's TAO emission share is proportional to their normalised weight.

---

## Bidding Strategy

The miner uses a configurable strategy to stay profitable:

```
initial_bid_percentage = 0.05   →  initial bid  = debt × 1.05
bid_increment_rate     = 0.05   →  rebid        = current_highest × 1.05
max_bid_percentage     = 0.95   →  max bid      = debt × 1.95
max_bid_absolute       = None   →  no absolute token cap (optional override)
min_profit_margin      = 0.02   →  reject bid if profit < 2% of collateral_value
```

Since the liquidation ratio is `1.2`, the collateral is worth at least `1.2 × debt` at auction time. The theoretical maximum spread available to a miner is therefore:

```
max possible profit = collateral_value - debt  ≥  (1.2 × debt) - debt  =  0.2 × debt
```

`max_bid_percentage = 0.95` is a ceiling. `min_profit_margin = 0.02` is a profitability guard. The bid is rejected before submission if:

```
collateral_value - bid  <  collateral_value × 0.02
```

The miner automatically re-evaluates and counter-bids whenever it is outbid, up to the configured cap.

---

## How to Run

```bash
uv run neurons/miner.py \
  --netuid 421 \
  --subtensor.network test \
  --wallet.name <your_coldkey> \
  --wallet.hotkey <your_hotkey> \
  --logging.info
```

| Flag | Description |
|---|---|
| `--netuid 421` | Subnet UID to register on |
| `--subtensor.network test` | Connect to the testnet |
| `--wallet.name test` | Name of the local wallet |
| `--wallet.hotkey default` | Hotkey to use for this miner |
| `--logging.info` | Enable info-level logs |
