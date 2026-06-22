#!/usr/bin/env python3
"""
Minimal TensorUSD miner agent that satisfies all format requirements.

Usage (sandbox):
    python agent.py --infer    # run inference → /data/output/output.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# AgentMiner — required class with infer() only
# ---------------------------------------------------------------------------


class AgentMiner:
    """
    Miner agent that produces a prediction CSV output file.

    The output CSV must match the expected ground-truth schema:

        snapshot_hour, snapshot_time_utc, block_number,
        vault_owner, vault_id, vault_health, tokens_minted
    """

    def __init__(self) -> None:
        """Initialize any resources needed for inference."""
        # Placeholder: a real agent would setup apis, setup agents.
        pass

    def infer(self, output_path: str = "/data/output/output.csv") -> None:
        """
        Run inference and write a CSV prediction file.

        The output must match the ground-truth schema:

            snapshot_hour, snapshot_time_utc, block_number,
            vault_owner, vault_id, vault_health, tokens_minted

        This is a placeholder that writes a headers-only CSV.
        """
        print("[infer] Running inference...", flush=True)

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "snapshot_hour",
            "snapshot_time_utc",
            "block_number",
            "vault_owner",
            "vault_id",
            "vault_health",
            "tokens_minted",
        ]

        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

        print(f"[infer] Wrote header-only CSV → {output_path}", flush=True)


# ---------------------------------------------------------------------------
# main() — required top-level entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point: parse args and run inference."""
    parser = argparse.ArgumentParser(description="TensorUSD Agent")
    parser.add_argument("--infer", action="store_true", help="Run inference phase")
    args = parser.parse_args()

    agent = AgentMiner()

    if args.infer:
        agent.infer()
    else:
        print("Usage: python agent.py --infer", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
