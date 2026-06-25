import threading
import time
from typing import Optional

import requests
import bittensor as bt

from typing import TYPE_CHECKING

import concurrent.futures
from decimal import Decimal

from tensorusd.common.contract import TensorUSDPriceOracleContract

if TYPE_CHECKING:
    from neurons.miner.oracle import OracleMiner

CMC_QUOTES_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
PRICE_DECIMALS = 10**18


def fetch_tao_price_usd(api_key: str) -> Optional[float]:
    try:
        bt.logging.debug(f"Fetching tao price from CoinMarketCap...")

        response = requests.get(
            CMC_QUOTES_URL,
            params={"slug": "bittensor", "convert": "USD"},
            headers={
                "Accepts": "application/json",
                "X-CMC_PRO_API_KEY": api_key,
            },
            timeout=10.0,
        )
        response.raise_for_status()

        payload = response.json()
        data = payload.get("data") or {}
        coin = next(iter(data.values()), None)

        if not coin:
            bt.logging.error("Invalid CoinMarketCap quotes data: no coin data found")
            return None

        price_usd = float(coin["quote"]["USD"]["price"])

        if price_usd <= 0:
            bt.logging.error(f"Invalid price received: {price_usd}")
            return None

        bt.logging.info(f"Fetched tao price: ${price_usd:.6f}")
        return price_usd

    except requests.HTTPError as e:
        bt.logging.error(f"HTTP error fetching price from CoinMarketCap: {e}")
        return None
    except (KeyError, TypeError, ValueError) as e:
        bt.logging.error(f"Error parsing CoinMarketCap response: {e}")
        return None
    except Exception as e:
        bt.logging.error(f"Unexpected error fetching price: {e}")
        return None


class PriceOracleMiner:
    def __init__(
        self, miner: "OracleMiner", oracle_contract: TensorUSDPriceOracleContract
    ):
        self.miner = miner

        self.is_running: bool = False
        self.future: Optional[concurrent.futures.Future] = None
        self.stop_event = threading.Event()

        self.last_submission_time: Optional[float] = None
        self.oracle_contract = oracle_contract
        self.last_oracle_price = None
        self.step = 0

    def price_change_percent(self, current_price: int, reference_price: int) -> float:
        if reference_price <= 0:
            raise ValueError("Reference price must be greater than zero")

        return abs(current_price - reference_price) / reference_price

    def should_submit_price(self, current_price: float) -> tuple[bool, str]:
        now = time.time()
        if self.step > 1:
            self.last_oracle_price = self.oracle_contract.get_latest_price()
            bt.logging.info(
                f"Fetched latest price from chain : {self.last_oracle_price/PRICE_DECIMALS} TAO"
            )
            self.step = 0
        elif self.step == 1:
            self.step += 1
        # First run: submit immediately
        if self.last_submission_time is None:
            self.last_oracle_price = self.oracle_contract.get_latest_price()
            bt.logging.info(
                f"Fetched latest price initially from chain : ${self.last_oracle_price/PRICE_DECIMALS}"
            )
            return True, "initial submission"

        seconds_since_last_submission = now - self.last_submission_time

        # Rule 1: force submit every 6 hours no matter what
        if (
            seconds_since_last_submission
            >= self.miner.config.price.submission_interval_seconds
        ):
            self.step += 1
            return True, "force submission interval reached"

        # Rule 2: submit early if price moved by threshold %
        change_percent = self.price_change_percent(
            current_price=int(Decimal(str(current_price)) * PRICE_DECIMALS),
            reference_price=self.last_oracle_price,
        )

        if change_percent >= self.miner.config.price.change_threshold:
            return True, f"price changed by {change_percent * 100:.2f}%"

        return False, f"price change only {change_percent * 100:.2f}%"

    def wait_or_stop(self, seconds: int) -> bool:
        return self.stop_event.wait(timeout=seconds)

    def submit_price(self, price_usd: float) -> bool:
        price_ratio = int(Decimal(str(price_usd)) * PRICE_DECIMALS)

        bt.logging.info(f"Submitting price to oracle: {price_ratio}")

        tx_hash = self.miner.oracle_contract.submit_price(
            price=price_ratio,
            provider=self.miner.config.price.provider,
            keypair=self.miner.wallet.coldkey,  # type: ignore
        )

        if tx_hash:
            bt.logging.success(f"Price submitted successfully! Tx hash: {tx_hash}")
            self.last_submission_time = time.time()
            return True

        bt.logging.error("Price submission failed - no transaction hash returned")
        return False

    def run(self):
        bt.logging.info("Starting price oracle miner...")

        while not self.stop_event.is_set():
            try:
                price_usd = fetch_tao_price_usd(
                    self.miner.config.cmc.api_key  # type: ignore
                )

                if price_usd is None:
                    bt.logging.error("Failed to fetch price, skipping this cycle")

                    if self.wait_or_stop(
                        self.miner.config.price.monitor_interval_seconds
                    ):
                        break

                    continue

                should_submit, reason = self.should_submit_price(price_usd)

                bt.logging.info(
                    f"Current TAO price: ${price_usd}. Submission check: {reason}"
                )

                if should_submit:
                    try:
                        self.submit_price(price_usd)
                    except Exception as e:
                        bt.logging.error(f"Error submitting price to oracle: {e}")
                else:
                    bt.logging.info(
                        f"No submission needed. Checking again in "
                        f"{self.miner.config.price.monitor_interval_seconds} seconds."
                    )

                if self.wait_or_stop(self.miner.config.price.monitor_interval_seconds):
                    break

            except Exception as e:
                bt.logging.error(f"Unexpected error in price oracle loop: {e}")
                bt.logging.info("Retrying after 60 seconds...")

                if self.wait_or_stop(60):
                    break

        bt.logging.info("Price oracle miner stopped.")

    def run_in_executor(self, executor: concurrent.futures.ThreadPoolExecutor):
        if self.is_running:
            bt.logging.warning("Price oracle miner is already running.")
            return

        bt.logging.debug("Starting price oracle miner in executor.")

        self.stop_event.clear()
        self.future = executor.submit(self.run)
        self.is_running = True

    def stop(self):
        if not self.is_running:
            return

        bt.logging.debug("Stopping price oracle miner.")

        self.stop_event.set()

        if self.future is not None:
            try:
                self.future.result(timeout=5)
            except concurrent.futures.TimeoutError:
                bt.logging.warning("Price oracle miner did not stop within 5 seconds.")
            except Exception as e:
                bt.logging.error(f"Error while stopping price oracle miner: {e}")

        self.is_running = False
