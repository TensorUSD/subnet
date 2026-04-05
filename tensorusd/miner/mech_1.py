import threading
import time
from typing import Optional

from bittensor_wallet import Keypair
import requests
import bittensor as bt

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from neurons.miner import Miner

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
    def __init__(self, miner: "Miner"):
        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.miner = miner

    def run(self):
        bt.logging.info("Starting price oracle miner...")
        submission_count = 0
        while True:
            try:
                bt.logging.info(f"\n--- Price Submission #{submission_count + 1} ---")
                price_usd = fetch_tao_price_usd(self.miner.config.cmc.api_key)  # type: ignore
                if price_usd is None:
                    bt.logging.error("Failed to fetch price, skipping submission")
                    time.sleep(self.miner.config.price.ubmission_interval_seconds)  # type: ignore
                    continue
                price_ratio = int(price_usd * PRICE_DECIMALS)
                try:
                    bt.logging.info(f"Submitting price to oracle: {price_ratio}")

                    tx_hash = self.miner.oracle_contract.submit_price(
                        price=price_ratio,
                        keypair=self.miner.wallet.coldkey,  # type: ignore
                    )
                    if tx_hash:
                        bt.logging.success(
                            f"Price submitted successfully! Tx hash: {tx_hash}"
                        )
                        return True
                    else:
                        bt.logging.error(
                            "Price submission failed - no transaction hash returned"
                        )
                        return False

                except Exception as e:
                    bt.logging.error(f"Error submitting price to oracle: {e}")

                time.sleep(self.miner.config.price.submission_interval_seconds)  # type: ignore

            except KeyboardInterrupt:
                bt.logging.info("Received shutdown signal, stopping miner...")
                break
            except Exception as e:
                bt.logging.error(f"Unexpected error in main loop: {e}")
                bt.logging.info("Continuing after error...")
                time.sleep(60)  # Wait 1 minute before retrying after error

    def run_in_background_thread(self):
        """Start listener in background thread."""
        if not self.is_running:
            bt.logging.debug("Starting event listener in background thread.")
            self.should_exit = False
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True

    def stop_run_thread(self):
        """Stop the background thread."""
        if self.is_running:
            bt.logging.debug("Stopping event listener.")
            self.should_exit = True
            if self.thread is not None:
                self.thread.join(5)
            self.is_running = False
