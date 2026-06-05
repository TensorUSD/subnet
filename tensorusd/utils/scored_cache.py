"""
tensorusd/utils/scored_cache.py --> Persistent, bounded cache of already-scored submission IDs.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from collections import deque
from pathlib import Path  # noqa: TC003

from tensorusd.utils.logging import get_logger

log = get_logger(__name__)


class ScoredCache:
    """
    Thread-safe, disk-backed set of already-evaluated submission IDs.
    """

    def __init__(self, path: Path, max_size: int = 10_000) -> None:
        if max_size < 1:
            raise ValueError("max_size must be ≥ 1")

        self._path = path
        self._max_size = max_size
        self._lock = threading.RLock()

        # Insertion-ordered deque for O(1) eviction; set for O(1) lookup.
        self._order: deque[str] = deque()
        self._ids: set[str] = set()

        self._load()

    def __contains__(self, submission_id: str) -> bool:
        """Return True if *submission_id* is in the cache."""
        with self._lock:
            return submission_id in self._ids

    def add(self, submission_id: str) -> None:
        """
        Mark *submission_id* as scored.
        """
        with self._lock:
            if submission_id in self._ids:
                return  # already present — nothing to do

            # Evict oldest if at capacity
            if len(self._ids) >= self._max_size:
                oldest = self._order.popleft()
                self._ids.discard(oldest)
                log.debug("ScoredCache evicted oldest entry: %s", oldest)

            self._order.append(submission_id)
            self._ids.add(submission_id)

        # Flush outside the lock to minimise lock-hold time
        self._flush()

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)

    # Persistence

    def _load(self) -> None:
        """Load the cache from disk. Silently starts empty on any error."""
        with self._lock:
            if not self._path.exists():
                log.debug("ScoredCache: no existing file at %s — starting empty.", self._path)
                return

            try:
                raw = self._path.read_text(encoding="utf-8")
                items: list[str] = json.loads(raw)

                if not isinstance(items, list):
                    raise ValueError("Expected a JSON array")

                # Respect max_size: keep only the most recent entries
                trimmed = items[-self._max_size :]
                self._order = deque(trimmed)
                self._ids = set(trimmed)

                log.info(
                    "ScoredCache: loaded %d submission IDs from %s.",
                    len(self._ids),
                    self._path,
                )

            except Exception as exc:
                log.warning(
                    "ScoredCache: could not load %s (%s) — starting empty.",
                    self._path,
                    exc,
                )
                self._order = deque()
                self._ids = set()

    def _flush(self) -> None:
        """
        Atomically write the current cache to disk.
        """
        with self._lock:
            items = list(self._order)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")

        try:
            tmp_path.write_text(json.dumps(items, indent=None), encoding="utf-8")
            # os.replace is atomic on POSIX; on Windows it overwrites atomically
            os.replace(tmp_path, self._path)
        except Exception as exc:
            log.error("ScoredCache: failed to flush to %s: %s", self._path, exc)
            with contextlib.suppress(Exception):
                tmp_path.unlink(missing_ok=True)
