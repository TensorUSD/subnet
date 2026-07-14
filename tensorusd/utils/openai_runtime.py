"""Helpers for creating a budget-aware OpenAI runtime inside the sandbox."""

from __future__ import annotations

import os

from tensorusd.utils.openai_utils import create_tracked_openai_client
from tensorusd.utils.pricing import get_model_pricing


def build_agent_openai_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for inference.")

    model_id = os.environ.get("TENSORUSD_MODEL_ID")
    if not model_id:
        raise RuntimeError("TENSORUSD_MODEL_ID is required for inference.")

    token_budget = int(os.environ.get("TENSORUSD_SANDBOX_TOKEN_BUDGET", "200000"))
    cost_budget_raw = os.environ.get("TENSORUSD_SANDBOX_COST_BUDGET_USD")
    cost_budget_usd = float(cost_budget_raw) if cost_budget_raw else None
    pricing = get_model_pricing(model_id)

    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("The openai package is required in the sandbox.") from exc

    sdk_client = OpenAI(api_key=api_key)

    def request_fn(*, endpoint: str, model: str, **kwargs):
        if endpoint == "responses.create":
            return sdk_client.responses.create(model=model, **kwargs)
        if endpoint == "chat.completions.create":
            return sdk_client.chat.completions.create(model=model, **kwargs)
        raise ValueError(f"Unsupported endpoint: {endpoint}")

    return model_id, create_tracked_openai_client(
        request_fn=request_fn,
        allowed_models=(model_id,),
        token_budget=token_budget,
        cost_budget_usd=cost_budget_usd,
        pricing=pricing,
    )