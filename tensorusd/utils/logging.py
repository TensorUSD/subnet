from __future__ import annotations
import os
import logging
from logging.handlers import RotatingFileHandler


import logging.handlers
import sys
from pathlib import Path

from rich.logging import RichHandler

from tensorusd.auth.config import settings

EVENTS_LEVEL_NUM = 38
DEFAULT_LOG_BACKUP_COUNT = 10
_configured = False

_FILE_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
_FILE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

def setup_events_logger(full_path, events_retention_size):
    logging.addLevelName(EVENTS_LEVEL_NUM, "EVENT")

    logger = logging.getLogger("event")
    logger.setLevel(EVENTS_LEVEL_NUM)
    logger.propagate = False

    def event(self, message, *args, **kws):
        if self.isEnabledFor(EVENTS_LEVEL_NUM):
            self._log(EVENTS_LEVEL_NUM, message, args, **kws)

    logging.Logger.event = event

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        os.path.join(full_path, "events.log"),
        maxBytes=events_retention_size,
        backupCount=DEFAULT_LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(EVENTS_LEVEL_NUM)
    logger.addHandler(file_handler)

    return logger



def get_logger(name: str) -> logging.Logger:
    """
    Return a logger
    """
    global _configured  # noqa: PLW0603

    root = logging.getLogger("tensorusd")

    if not _configured:
        level = logging.getLevelName(settings.log_level.upper())

        console_handler = RichHandler(
            level=level,
            rich_tracebacks=True,
            show_path=False,
            markup=True,
        )
        console_handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))

        root.setLevel(level)
        root.addHandler(console_handler)

        if settings.log_file_enabled:
            try:
                log_dir: Path = settings.log_dir
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = log_dir / settings.log_filename

                file_handler = logging.handlers.RotatingFileHandler(
                    filename=log_path,
                    maxBytes=settings.logfile_max_bytes,
                    backupCount=settings.logs_backup_count,
                    encoding="utf-8",
                )
                file_handler.setLevel(level)
                file_handler.setFormatter(
                    logging.Formatter(_FILE_FORMAT, datefmt=_FILE_DATE_FORMAT)
                )
                root.addHandler(file_handler)
                root.debug("File logging enabled → %s", log_path)

            except Exception as exc:  # noqa: BLE001
                root.warning("Could not set up file logging: %s", exc)

        if level > logging.DEBUG:
            for noisy in ("urllib3", "bittensor", "docker"):
                logging.getLogger(noisy).setLevel(logging.WARNING)

        _configured = True

    return logging.getLogger(name)