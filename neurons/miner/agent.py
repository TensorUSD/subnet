#!/usr/bin/env python3
"""
TensorUSD miner agent.

The validator injects runtime metadata into the sandbox environment:
- OPENAI_API_KEY from .env / container env
- TENSORUSD_MODEL_ID from the submission's /unevaluated payload
- TENSORUSD_SANDBOX_TOKEN_BUDGET / TENSORUSD_SANDBOX_COST_BUDGET_USD

This agent uses the wrapper provided by tensorusd.utils.openai_utils so the
OpenAI session is budgeted and model-allowlisted.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

from tensorusd.utils.openai_runtime import build_agent_openai_client
from tensorusd.utils.pricing import estimate_openai_cost_usd, get_model_pricing


class AgentMiner:
    FIELDNAMES = [
        "snapshot_hour",
        "snapshot_time_utc",
        "block_number",
        "vault_owner",
        "vault_id",
        "vault_health",
        "tokens_minted",
    ]

    def __init__(self) -> None:
        self.model_id, self.client = build_agent_openai_client()
        self.pricing = get_model_pricing(self.model_id)

    @staticmethod
    def _log(message: str) -> None:
        print(f"[agent] {message}", flush=True)

    @staticmethod
    def _parse_json_payload(content: str) -> dict:
        import json

        try:
            payload = json.loads(content)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        """Best-effort extraction of text from SDK or dict responses."""
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        if isinstance(response, dict):
            if isinstance(response.get("output_text"), str):
                return response["output_text"]

            output = response.get("output") or []
            text_chunks: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                for content_item in item.get("content", []):
                    if not isinstance(content_item, dict):
                        continue
                    text_value = content_item.get("text")
                    if isinstance(text_value, str):
                        text_chunks.append(text_value)
            return "\n".join(text_chunks).strip()

        return ""

    def _log_openai_success(
        self,
        response: Any,
        content: str,
        payload: dict,
        *,
        label: str,
    ) -> None:
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")

        call_prompt_tokens = 0
        call_completion_tokens = 0
        if isinstance(usage, dict):
            call_prompt_tokens = int(
                usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
            )
            call_completion_tokens = int(
                usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
            )
        elif usage is not None:
            call_prompt_tokens = int(
                getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0)) or 0
            )
            call_completion_tokens = int(
                getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0)) or 0
            )

        call_cost_usd = estimate_openai_cost_usd(
            input_tokens=call_prompt_tokens,
            output_tokens=call_completion_tokens,
            pricing=self.pricing,
            safety_multiplier=1.0,
        )

        tracked_usage = None
        try:
            tracked_usage = self.client.usage()
        except Exception:
            tracked_usage = None

        remaining_cost_usd = None
        try:
            remaining_cost_usd = self.client.remaining_cost_usd()
        except Exception:
            remaining_cost_usd = None

        self._log(
            "OpenAI call succeeded "
            f"label={label} "
            f"model={self.model_id} "
            f"content_len={len(content)} "
            f"parsed_keys={sorted(payload.keys()) if payload else []}"
        )

        if usage is not None:
            self._log(f"OpenAI usage={usage}")

        if tracked_usage is not None:
            self._log(
                "Tracked token usage "
                f"prompt_tokens={tracked_usage.prompt_tokens} "
                f"completion_tokens={tracked_usage.completion_tokens} "
                f"total_tokens={tracked_usage.total_tokens}"
            )

        self._log(
            "Estimated call cost "
            f"label={label} "
            f"usd={call_cost_usd:.6f} "
            f"prompt_tokens={call_prompt_tokens} "
            f"completion_tokens={call_completion_tokens}"
        )

        if remaining_cost_usd is not None:
            remaining_cost_text = (
                f"{remaining_cost_usd:.6f}"
                if remaining_cost_usd != float("inf")
                else "inf"
            )
            self._log(f"Remaining sandbox cost budget usd={remaining_cost_text}")

        if content:
            preview = content[:300].replace("\n", " ")
            self._log(f"OpenAI response preview={preview}")

    def _call_openai(self, prompt: str, *, label: str) -> dict:
        self._log(f"Starting OpenAI request [{label}] with model={self.model_id}")
        try:
            response = self.client.responses_create(model=self.model_id, input=prompt)
            content = self._extract_response_text(response)
            payload = self._parse_json_payload(content)
            self._log_openai_success(response, content, payload, label=label)
            if not payload:
                self._log(
                    f"OpenAI response [{label}] did not parse into JSON object; CSV will use empty defaults"
                )
            return payload
        except Exception as exc:
            self._log(f"OpenAI call failed [{label}]: {type(exc).__name__}: {exc}")
            raise

    @classmethod
    def _normalize_row(cls, payload: dict) -> dict:
        return {k: payload.get(k, "") for k in cls.FIELDNAMES}

    def _build_prompt(self, *, row_index: int, previous_rows: list[dict]) -> str:
        previous_summary = "none"
        if previous_rows:
            previous_summary = "; ".join(
                f"{row.get('snapshot_hour', '?')}|{row.get('vault_id', '?')}|{row.get('tokens_minted', '?')}"
                for row in previous_rows
            )

        return (
            "Return only a JSON object with keys snapshot_hour, snapshot_time_utc, "
            "block_number, vault_owner, vault_id, vault_health, tokens_minted. "
            f"Generate row #{row_index}. Ensure vault_id is unique. "
            f"Previous rows: {previous_summary}. "
            "Use plausible placeholder values if needed."
        )

    def _generate_rows(self, row_count: int = 3) -> list[dict]:
        rows: list[dict] = []
        for idx in range(1, row_count + 1):
            payload = self._call_openai(
                self._build_prompt(row_index=idx, previous_rows=rows),
                label=f"row-{idx}",
            )
            row = self._normalize_row(payload)
            rows.append(row)
            self._log(
                f"Generated row-{idx} vault_id={row.get('vault_id')} tokens_minted={row.get('tokens_minted')}"
            )
        return rows

    def infer(self, output_path: str = "/data/output/output.csv") -> None:
        self._log(f"Running inference with model={self.model_id}")
        rows = self._generate_rows(row_count=3)

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)

        tracked_usage = self.client.usage()
        total_estimated_cost_usd = estimate_openai_cost_usd(
            input_tokens=tracked_usage.prompt_tokens,
            output_tokens=tracked_usage.completion_tokens,
            pricing=self.pricing,
            safety_multiplier=1.0,
        )
        self._log(
            "Session cumulative token usage "
            f"prompt_tokens={tracked_usage.prompt_tokens} "
            f"completion_tokens={tracked_usage.completion_tokens} "
            f"total_tokens={tracked_usage.total_tokens}"
        )
        self._log(f"Session estimated cumulative cost usd={total_estimated_cost_usd:.6f}")
        self._log(f"Wrote CSV with {len(rows)} rows → {output_path}")


def main() -> None:
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