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


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "")
    if not val:
        return default
    return val.lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Settings:
    # Bittensor
    network: str = field(default_factory=lambda: _env("BITTENSOR_NETWORK", "test"))
    netuid: int = field(default_factory=lambda: _env_int("TENSORUSD_NETUID", 421))

    # Backend
    backend_url: str = field(
        default_factory=lambda: _env("TENSORUSD_SN_BACKEND_URL", "http://localhost:8000").rstrip("/")
    )
    http_timeout: int = field(default_factory=lambda: _env_int("TENSORUSD_HTTP_TIMEOUT", 30))

    # Validator loop
    validator_poll_interval: int = field(
        default_factory=lambda: _env_int("TENSORUSD_VALIDATOR_POLL_INTERVAL", 60)
    )
    weight_interval_blocks: int = field(
        default_factory=lambda: _env_int("TENSORUSD_WEIGHT_INTERVAL_BLOCKS", 100)
    )
    weight_monitor_interval: int = field(
        default_factory=lambda: _env_int("TENSORUSD_WEIGHT_MONITOR_INTERVAL", 300)
    )

    # Best-agent cache
    best_agent_dir: Path = field(
        default_factory=lambda: Path(_env("TENSORUSD_BEST_AGENT_DIR", ".TENSORUSD_cache/best_agent"))
    )
    best_agent_poll_interval: int = field(
        default_factory=lambda: _env_int("TENSORUSD_BEST_AGENT_POLL_INTERVAL", 60)
    )

    # Sandbox
    sandbox_image: str = field(
        default_factory=lambda: _env("TENSORUSD_SANDBOX_IMAGE", "TENSORUSD-sandbox:latest")
    )
    sandbox_workdir: Path = field(
        default_factory=lambda: Path(_env("TENSORUSD_SANDBOX_WORKDIR", "/tmp/TENSORUSD_sandbox"))
    )

    # Logging
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    log_dir: Path = field(default_factory=lambda: Path(_env("TENSORUSD_LOG_DIR", "logs")))
    log_file_enabled: bool = field(
        default_factory=lambda: _env_bool("TENSORUSD_LOG_FILE_ENABLED", True)
    )
    log_filename: Path = field(
        default_factory=lambda: Path(_env("LOG_FILENAME", "tensorusd_vali.log"))
    )

    logfile_max_bytes: int = field(
        default_factory=lambda: _env_int("LOGFILE_MAX_BYTES", 10 * 1024 * 1024)
    )
    logs_backup_count: int = field(default_factory=lambda: _env_int("LOGS_BACKUP_COUNT", 7))

    # Plagiarism threshold
    plagiarism_threshold: float = field(default_factory=lambda: _env_float("PLAGIARISM_THRESHOLD", 0.95))

    # Docker config
    setup_timeout: int = field(default_factory=lambda: _env_int("SETUP_TIMEOUT", 600))
    infer_timeout: int = field(default_factory=lambda: _env_int("INFER_TIMEOUT", 1500))

    memory_limit: str = field(default_factory=lambda: _env("MEMORY_LIMIY", "16g"))

    # Docker CPU cap — prevents runaway agents from pinning validator CPUs
    cpu_limit: float = field(
        default_factory=lambda: float(os.environ.get("CPU_LIMIT", 4.0))
    )

    # Burn
    burn_mode: bool = field(default_factory=lambda: _env_bool("BURN_MODE", True))

    # Scored-submissions persistence
    scored_cache_path: Path = field(
        default_factory=lambda: Path(
            _env("TENSORUSD_SCORED_CACHE_PATH", ".TENSORUSD_cache/scored_submissions.json")
        )
    )
    # Maximum number of submission IDs to retain in the scored cache.
    scored_cache_max_size: int = field(
        default_factory=lambda: _env_int("TENSORUSD_SCORED_CACHE_MAX_SIZE", 100)
    )

    # Allowed outbound hosts for the setup-phase network (allowlist-only egress).
    setup_allowed_hosts: str = field(
        default_factory=lambda: _env("TENSORUSD_SETUP_ALLOWED_HOSTS", "")
    )

    # Ground-truth generation
    ground_truth_dir: Path = field(
        default_factory=lambda: Path(
            _env("TENSORUSD_GROUND_TRUTH_DIR", "ground-truth")
        )
    )
    scoring_delay_days: int = field(
        default_factory=lambda: _env_int("TENSORUSD_SCORING_DELAY_DAYS", 7)
    )

    # Delayed evaluation (Phase 2 scoring)
    scoring_poll_interval: int = field(
        default_factory=lambda: _env_int("TENSORUSD_SCORING_POLL_INTERVAL", 120)
    )
    scoring_timeout: int = field(
        default_factory=lambda: _env_int("TENSORUSD_SCORING_TIMEOUT", 60)
    )


settings = Settings()