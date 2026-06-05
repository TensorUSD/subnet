"""
On-chain weight assignment for the Tensorusd subnet.
Supports normal best-agent mode and 100% burn mode (weights always to UID 0).
"""

from __future__ import annotations

import threading
import time
from typing import Any

import bittensor as bt
from tenacity import (
    RetryError,
    retry,
    stop_after_attempt,
    wait_exponential,
)

from tensorusd.auth.config import settings
from tensorusd.utils.logging import get_logger

log = get_logger(__name__)

_RETRY_KWARGS = dict(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=4, max=120),
    reraise=True,
)


class WeightMonitor(threading.Thread):
    """
    Background daemon thread that logs on-chain weights every
    TENSORUSD_WEIGHT_MONITOR_INTERVAL seconds.
    """

    daemon = True

    def __init__(
        self,
        wallet: bt.Wallet,
        subtensor: bt.Subtensor,
        metagraph: bt.Metagraph,
        interval: int | None = None,
    ) -> None:
        super().__init__(name="WeightMonitor")
        self._wallet = wallet
        self._subtensor = subtensor
        self._metagraph = metagraph
        self._interval = interval or settings.weight_monitor_interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("WeightMonitor started (reporting every %ds)", self._interval)
        # Wait one full interval before first report — avoid noise at startup
        self._stop_event.wait(timeout=self._interval)

        while not self._stop_event.is_set():
            try:
                self._report()
            except Exception as exc:
                log.warning("WeightMonitor error: %s", exc)
            self._stop_event.wait(timeout=self._interval)

        log.info("WeightMonitor stopped.")

    def _report(self) -> None:
        """Fetch and log current on-chain weights set by this validator."""
        try:
            self._metagraph.sync(subtensor=self._subtensor)
        except Exception as exc:
            log.warning("WeightMonitor: metagraph sync failed: %s", exc)
            return

        hotkey = self._wallet.hotkey.ss58_address
        if hotkey not in self._metagraph.hotkeys:
            log.warning("WeightMonitor: validator hotkey not in metagraph.")
            return

        validator_uid = self._metagraph.hotkeys.index(hotkey)
        all_uids = list(self._metagraph.uids)
        weights_matrix = self._metagraph.weights

        if weights_matrix is None or len(weights_matrix) == 0:
            log.info(
                "── Weight Monitor (validator uid=%d) ──\n"
                "  No weights set on-chain yet — waiting for first weight-set event.",
                validator_uid,
            )
            return

        if validator_uid >= len(weights_matrix):
            log.info(
                "── Weight Monitor (validator uid=%d) ──\n"
                "  Weights matrix has %d rows — this validator has not set weights yet.",
                validator_uid,
                len(weights_matrix),
            )
            return

        weights = [float(w) for w in weights_matrix[validator_uid]]
        if len(weights) != len(all_uids):
            weights = (weights + [0.0] * len(all_uids))[: len(all_uids)]

        nonzero = [
            (int(uid), round(w, 4)) for uid, w in zip(all_uids, weights, strict=False) if w > 0.0
        ]
        log.info(
            "── Weight Monitor (validator uid=%d) ──\n  Total UIDs : %d\n  Non-zero   : %s",
            validator_uid,
            len(all_uids),
            nonzero if nonzero else "none yet — weights not set yet",
        )
        log.debug("UIDs array:     %s", all_uids)
        log.debug("Weights array:  %s", weights)


class WeightSetter:
    """Manages on-chain weight setting. Supports normal best-agent mode and 100% BURN mode."""

    def __init__(
        self,
        wallet: bt.Wallet,
        subtensor: bt.Subtensor,
        metagraph: bt.Metagraph,
    ) -> None:
        self._wallet = wallet
        self._subtensor = subtensor
        self._metagraph = metagraph
        try:
            self._last_set_block: int = subtensor.get_current_block()
            log.info("WeightSetter: seeded last_set_block=%d at startup.", self._last_set_block)
        except Exception as exc:
            log.warning("WeightSetter: could not seed last_set_block: %s — using 0.", exc)
            self._last_set_block = 0
        self._monitor = WeightMonitor(wallet, subtensor, metagraph)

    def start_monitor(self) -> None:
        self._monitor.start()

    def stop_monitor(self) -> None:
        self._monitor.stop()

    def maybe_set_weights(self, best_hotkey: str | None = None) -> bool:
        """
        Entry point called after every evaluation cycle.
        Never raises — all failures are logged and the validator keeps running.
        """
        current_block = self._current_block()

        if settings.burn_mode:
            blocks_since = current_block - self._last_set_block
            if blocks_since < settings.weight_interval_blocks:
                log.info(
                    "BURN MODE: weight tempo not met — %d/%d blocks elapsed since last set "
                    "(block %d). Will retry in ~%d more blocks.",
                    blocks_since,
                    settings.weight_interval_blocks,
                    self._last_set_block,
                    settings.weight_interval_blocks - blocks_since,
                )
                return False

            log.info(
                "BURN MODE active — forcing weights to UID 0 (100%% burn) at block %d",
                current_block,
            )
            return self._safe_call(self._burn_with_retry, label="burn")

        # Normal mode
        if not best_hotkey:
            log.info("No best hotkey available — skipping normal weight set.")
            return False

        blocks_since = current_block - self._last_set_block
        log.info(
            "Normal weight set check — best_hotkey=%s  current_block=%d  "
            "last_set_block=%d  blocks_since=%d  interval=%d",
            best_hotkey,
            current_block,
            self._last_set_block,
            blocks_since,
            settings.weight_interval_blocks,
        )

        if blocks_since < settings.weight_interval_blocks:
            log.info(
                "Weight set gated: %d/%d blocks elapsed — will set in ~%d more blocks.",
                blocks_since,
                settings.weight_interval_blocks,
                settings.weight_interval_blocks - blocks_since,
            )
            return False

        log.info("Weight set interval cleared — proceeding to set weights (normal mode).")
        return self._safe_call(self._normal_with_retry, best_hotkey, label="normal")

    # Non-raising wrapper
    def _safe_call(self, fn, *args, label: str, **kwargs) -> bool:
        """
        Call *fn* with *args*. Catch RetryError and any other exception,
        log them, and return False so the evaluation loop is never interrupted.
        """
        try:
            return fn(*args, **kwargs)
        except RetryError as exc:
            last = exc.last_attempt.exception()
            log.error(
                "set_weights (%s) failed after all retries — last error: %s\n"
                "Validator continues; will retry on next cycle.",
                label,
                last,
            )
            return False
        except Exception as exc:
            log.error(
                "set_weights (%s) unexpected error: %s — validator continues.",
                label,
                exc,
            )
            return False

    # Shared low-level helpers
    def _current_block(self) -> int:
        try:
            return self._subtensor.get_current_block()
        except Exception as exc:
            log.warning("Could not fetch current block: %s", exc)
            return self._last_set_block

    def _sync_metagraph(self) -> None:
        try:
            self._metagraph.sync(subtensor=self._subtensor)
        except Exception as exc:
            log.warning("Metagraph sync failed: %s — proceeding with stale data.", exc)

    def _wait_for_new_block(self, start_block: int, timeout_s: int = 30) -> int:
        """
        Poll until the chain advances past *start_block*, then return the new
        block number. Falls back to returning whatever the chain reports after
        *timeout_s* seconds if no advancement is observed.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            new_block = self._current_block()
            if new_block > start_block:
                log.debug(
                    "Block advanced: %d → %d (waited %.1fs)",
                    start_block,
                    new_block,
                    timeout_s - (deadline - time.time()),
                )
                return new_block
            time.sleep(3)
        log.warning(
            "Timed out waiting for a new block after %ds — proceeding with current block.",
            timeout_s,
        )
        return self._current_block()

    def _refresh_substrate(self) -> None:
        """
        Force bittensor to refresh its internal substrate nonce cache.
        """
        try:
            self._subtensor.substrate.connect_websocket()
            log.debug("Substrate websocket refreshed — nonce cache reset.")
        except Exception:
            # connect_websocket not available on all bittensor versions;
            # fall back to a lightweight RPC ping that also refreshes the nonce.
            try:
                self._subtensor.substrate.get_block_number(None)
                log.debug("Substrate nonce cache refreshed via get_block_number.")
            except Exception as exc:
                log.warning("Could not refresh substrate connection: %s — proceeding.", exc)

    def _do_set_weights(
        self,
        all_uids: list[Any],
        weights: list[float],
        current_block: int,
        label: str,
    ) -> bool:
        """
        Call subtensor.set_weights and interpret possible failure modes.
        """
        log.debug(
            "[%s] Calling set_weights — block=%d  num_uids=%d",
            label,
            current_block,
            len(all_uids),
        )
        success, message = self._subtensor.set_weights(
            netuid=settings.netuid,
            wallet=self._wallet,
            uids=all_uids,
            weights=weights,
            wait_for_inclusion=True,
            wait_for_finalization=False,
        )

        if success:
            return True

        reason = (
            str(message)
            if message
            else (
                "chain returned (False, None) — likely a transient nonce race, "
                "stale block header, or RPC node internal timeout"
            )
        )
        log.warning("[%s] set_weights attempt failed: %s", label, reason)
        raise RuntimeError(f"set_weights [{label}]: {reason}")

    # Retry-decorated methods
    @retry(**_RETRY_KWARGS)
    def _normal_with_retry(self, best_hotkey: str) -> bool:
        self._refresh_substrate()
        current_block = self._current_block()
        self._sync_metagraph()

        uid_map: dict[str, int] = {
            self._metagraph.hotkeys[i]: int(self._metagraph.uids[i])
            for i in range(len(self._metagraph.uids))
        }

        if best_hotkey not in uid_map:
            log.warning(
                "Best hotkey %s not found in metagraph — may have deregistered. "
                "Skipping weight set.",
                best_hotkey,
            )
            return False

        best_uid = uid_map[best_hotkey]
        all_uids = list(self._metagraph.uids)
        weights = [0.0] * len(all_uids)
        weights[all_uids.index(best_uid)] = 1.0

        log.info(
            "Setting weights (normal) at block %d — best UID=%d  hotkey=%s",
            current_block,
            best_uid,
            best_hotkey,
        )

        ok = self._do_set_weights(all_uids, weights, current_block, label="normal")
        self._last_set_block = current_block
        log.info("Weights set successfully (normal) at block %d.", current_block)
        return ok

    @retry(**_RETRY_KWARGS)
    def _burn_with_retry(self) -> bool:
        self._refresh_substrate()
        current_block = self._current_block()
        self._sync_metagraph()

        all_uids = list(self._metagraph.uids)
        if not all_uids or 0 not in set(all_uids):
            log.warning("UID 0 not present in metagraph — cannot perform burn weight set.")
            return False

        weights = [0.0] * len(all_uids)
        weights[all_uids.index(0)] = 1.0

        log.info(
            "BURN MODE: Setting weights at block %d — UID 0 = 1.0  (all others 0.0)",
            current_block,
        )

        ok = self._do_set_weights(all_uids, weights, current_block, label="burn")
        self._last_set_block = current_block
        log.info("Burn weights set successfully at block %d.", current_block)
        return ok
