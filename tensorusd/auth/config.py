from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "")
    if not val:
        return default
    return val.lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Settings:
    # Bittensor
    network: str = field(default_factory=lambda: _env("BITTENSOR_NETWORK", "finney"))

    # Backend
    backend_url: str = field(
        default_factory=lambda: _env(
            "TENSORUSD_SN_BACKEND_URL", "http://localhost:8000"
        ).rstrip("/")
    )
    
    http_timeout: int = field(
        default_factory=lambda: _env_int("TENSORUSD_HTTP_TIMEOUT", 30)
    )

    sandbox_token_budget: int = field(
        default_factory=lambda: _env_int("TENSORUSD_SANDBOX_TOKEN_BUDGET", 1_000_000)
    )

    # Logging
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    log_dir: Path = field(
        default_factory=lambda: Path(_env("TENSORUSD_LOG_DIR", "logs"))
    )
    log_file_enabled: bool = field(
        default_factory=lambda: _env_bool("TENSORUSD_LOG_FILE_ENABLED", True)
    )
    log_filename: Path = field(
        default_factory=lambda: Path(_env("LOG_FILENAME", "tensorusd_vali.log"))
    )

    logfile_max_bytes: int = field(
        default_factory=lambda: _env_int("LOGFILE_MAX_BYTES", 10 * 1024 * 1024)
    )
    logs_backup_count: int = field(
        default_factory=lambda: _env_int("LOGS_BACKUP_COUNT", 7)
    )

    # Ground-truth generation
    ground_truth_dir: Path = field(
        default_factory=lambda: Path(_env("TENSORUSD_GROUND_TRUTH_DIR", "ground-truth"))
    )
    scoring_delay_days: int = field(
        default_factory=lambda: _env_int("TENSORUSD_SCORING_DELAY_DAYS", 7)
    )


settings = Settings()
