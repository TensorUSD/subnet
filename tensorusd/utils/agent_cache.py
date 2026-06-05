"""
Manages a local on-disk cache of the current best agent file.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from tensorusd.auth.config import settings
from tensorusd.utils.logging import get_logger

if TYPE_CHECKING:
    from tensorusd.utils.backend_client import BackendClient

log = get_logger(__name__)


@dataclass
class BestAgentMeta:
    """Metadata snapshot of the current best agent."""

    submission_id: str
    hotkey: str
    final_score: float


@dataclass
class BestAgentCache:
    """
    Thread-safe, lazily populated cache for the current best agent file.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _agent_path: Path | None = field(default=None, init=False)
    _best_meta: BestAgentMeta | None = field(default=None, init=False)

    @property
    def agent_path(self) -> Path | None:
        with self._lock:
            return self._agent_path

    @property
    def best_meta(self) -> BestAgentMeta | None:
        with self._lock:
            return self._best_meta

    def _update(self, meta: BestAgentMeta, content: bytes) -> None:
        """
        Atomically replace the cached file and metadata.
        """
        cache_dir = settings.best_agent_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

        tmp_path = cache_dir / f"_tmp_{uuid.uuid4().hex}.py"
        tmp_path.write_bytes(content)

        with self._lock:
            old_path = self._agent_path

            final_path = cache_dir / f"{meta.submission_id}.py"
            tmp_path.rename(final_path)

            self._agent_path = final_path
            self._best_meta = meta

        if old_path and old_path.exists() and old_path != final_path:
            try:
                old_path.unlink()
                log.debug("Deleted stale best-agent cache: %s", old_path)
            except OSError as exc:
                log.warning("Could not delete stale cache file %s: %s", old_path, exc)

        log.info(
            "Best-agent cache updated → submission=%s  score=%.4f  hotkey=%s",
            meta.submission_id,
            meta.final_score,
            meta.hotkey,
        )


class BestAgentWatcher(threading.Thread):
    """
    Background daemon thread that polls the backend and refreshes the cache
    when a new best agent is detected.
    """

    daemon = True  # dies automatically when the main process exits

    def __init__(
        self,
        client: BackendClient,
        cache: BestAgentCache,
        poll_interval: int | None = None,
    ) -> None:
        super().__init__(name="BestAgentWatcher")
        self._client = client
        self._cache = cache
        self._interval = poll_interval or settings.best_agent_poll_interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Signal the watcher to exit cleanly on the next loop iteration."""
        self._stop_event.set()

    def run(self) -> None:
        log.info("BestAgentWatcher started (poll interval=%ds)", self._interval)

        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as exc:  # noqa: BLE001
                # Never crash the watcher thread — log and continue
                log.error("BestAgentWatcher error: %s", exc, exc_info=True)

            self._stop_event.wait(timeout=self._interval)

        log.info("BestAgentWatcher stopped.")

    def _poll_once(self) -> None:
        """
        Check the backend for a new best agent.  Downloads and caches only
        when the submission ID has changed.
        """
        meta_raw = self._client.get_best_submission_meta()

        if meta_raw is None:
            log.debug("No best agent on the backend yet.")
            return

        log.debug("Best agent meta: %s", meta_raw)

        # Handle flexible field names from backend
        new_submission_id: str = meta_raw.get("submission_id") or meta_raw.get("id") or ""
        if not new_submission_id:
            log.warning(
                "Best agent response missing submission_id. Keys: %s", list(meta_raw.keys())
            )
            return

        best_hotkey: str = meta_raw.get("hotkey") or meta_raw.get("miner_hotkey") or ""
        if not best_hotkey:
            log.warning("Best agent response missing hotkey. Keys: %s", list(meta_raw.keys()))
            return

        final_score: float = float(meta_raw.get("final_score") or meta_raw.get("score") or 0.0)

        current = self._cache.best_meta

        if current is not None and current.submission_id == new_submission_id:
            log.debug("Best agent unchanged (%s), skipping download.", new_submission_id)
            return

        # New best detected —> download it
        log.info(
            "New best agent detected: submission=%s  score=%.4f  hotkey=%s",
            new_submission_id,
            final_score,
            best_hotkey,
        )
        content = self._client.download_best_agent_file()

        new_meta = BestAgentMeta(
            submission_id=new_submission_id,
            hotkey=best_hotkey,
            final_score=final_score,
        )
        self._cache._update(new_meta, content)  # noqa: SLF001
