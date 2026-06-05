"""
Shared authentication helpers used by both the validator and any tooling
that needs to authenticate against the TensorUSD backend.
"""

from __future__ import annotations

import os
import logging
import bittensor as bt
import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from tensorusd.auth.config import settings
from tensorusd.utils.logging import setup_events_logger

# Retrieve the event logger
event_logger = logging.getLogger("event")

# Automatically initialize event logger if not already configured in this process
if not event_logger.handlers:
    try:
        os.makedirs(settings.log_dir, exist_ok=True)
        setup_events_logger(str(settings.log_dir), settings.logfile_max_bytes)
    except Exception as e:
        bt.logging.warning(f"Could not initialize event logger using logging.py: {e}")


def _log_event(message: str) -> None:
    """Helper to log events using the custom 'event' logger from tensorusd/utils/logging.py."""
    try:
        event_logger.event(message)
    except AttributeError:
        # Fallback to Bittensor standard info logging if events logger attribute fails
        bt.logging.info(f"[EVENT] {message}")


def _is_transient(exc: BaseException) -> bool:

    """
    Only retry on network-level errors.
    Never retry on 4xx — those are permanent auth failures (no permit, blacklisted, etc.)
    """
    if isinstance(exc, requests.HTTPError):  # noqa: SIM102
        if exc.response is not None and exc.response.status_code < 500:
            return False  # 4xx = permanent, don't retry
    return isinstance(exc, (requests.ConnectionError, requests.Timeout, requests.HTTPError))


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def fetch_validator_nonce(hotkey_ss58: str) -> dict:
    """
    Call GET /v1/validator/nonce and return the full data payload.
    """
    url = f"{settings.backend_url}/v1/validator/nonce"
    _log_event(f"Fetching validator nonce for hotkey {hotkey_ss58}")

    try:
        r = requests.get(
            url,
            params={"hotkey": hotkey_ss58},
            timeout=settings.http_timeout,
        )

        if r.status_code == 403:
            _log_event(f"403 Forbidden — hotkey {hotkey_ss58} has no validator permit.")
            raise requests.HTTPError(
                f"403 Forbidden — hotkey {hotkey_ss58} has no validator permit on this subnet.",
                response=r,
            )

        r.raise_for_status()
        data = r.json()["data"]
        _log_event(f"Successfully fetched validator nonce for hotkey {hotkey_ss58} (expires at {data.get('expires_at')})")
        return data
    except Exception as exc:
        _log_event(f"Error fetching validator nonce for hotkey {hotkey_ss58}: {exc}")
        raise


def sign_message(wallet: bt.Wallet, message: str) -> str:
    """Sign *message* with the wallet's hotkey, return hex-encoded signature."""
    _log_event(f"Signing message for hotkey {wallet.hotkey.ss58_address}")
    sig: bytes = wallet.hotkey.sign(message.encode())
    sig_hex = sig.hex()
    _log_event(f"Message signed successfully by hotkey {wallet.hotkey.ss58_address} (sig: {sig_hex[:16]}...)")
    return sig_hex