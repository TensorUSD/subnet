"""Pricing helpers for estimating TensorUSD submission fees."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import requests


COINGECKO_TAO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"


@dataclass(frozen=True, slots=True)
class ModelPricing:
    model: str
    input_usd_per_million_tokens: float
    output_usd_per_million_tokens: float


DEFAULT_MODEL_PRICING: tuple[ModelPricing, ...] = (
    ModelPricing("gpt-5.6-terra", 2.5, 15.00),
    ModelPricing("gpt-5.6-luna", 1.00, 6.00),
    ModelPricing("gpt-5.4", 2.50, 15.00),
    ModelPricing("gpt-5.4-mini", 0.75, 4.50),
    ModelPricing("gpt-5.4-nano", 0.20, 1.25),
    ModelPricing("gpt-5-mini", 0.25, 2),
    ModelPricing("gpt-5-nano", 0.05, 0.40),
    ModelPricing("gpt-4.1", 2.00, 8.00),
    ModelPricing("gpt-4.1-mini", 0.40, 1.60),
    ModelPricing("gpt-4o-mini", 0.15, 0.60),
)


def get_known_model_pricing() -> tuple[ModelPricing, ...]:
    """Return the priced models in display order."""
    return DEFAULT_MODEL_PRICING


def get_model_pricing(model_name: str) -> ModelPricing:
    for pricing in DEFAULT_MODEL_PRICING:
        if pricing.model == model_name:
            return pricing
    raise ValueError(f"No pricing found for model '{model_name}'.")


def get_most_expensive_allowed_model(allowed_models: Iterable[str]) -> ModelPricing:
    allowed = {m for m in allowed_models}
    candidates = [p for p in DEFAULT_MODEL_PRICING if p.model in allowed]
    if not candidates:
        raise ValueError("No priced models are present in the allowed model list.")
    return max(
        candidates,
        key=lambda p: max(p.input_usd_per_million_tokens, p.output_usd_per_million_tokens),
    )


def estimate_openai_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    pricing: ModelPricing,
    safety_multiplier: float = 2.0,
) -> float:
    base_cost = (
        (input_tokens / 1_000_000.0) * pricing.input_usd_per_million_tokens
        + (output_tokens / 1_000_000.0) * pricing.output_usd_per_million_tokens
    )
    return base_cost * safety_multiplier


def estimate_openai_cost_usd_from_output_ceiling(
    *,
    input_tokens: int,
    output_tokens: int,
    pricing: ModelPricing,
    eval_multiplier: float = 3.0,
) -> float:
    """
    Conservative cost estimate using the model's output token rate as a ceiling.

    This intentionally overestimates by charging both input and output tokens at the
    output-token rate, then scaling by the number of planned evaluations.
    """
    base_tokens = input_tokens + output_tokens
    base_cost = (base_tokens / 1_000_000.0) * pricing.output_usd_per_million_tokens
    return base_cost * eval_multiplier


def fetch_tao_price_usd_coingecko() -> float:
    response = requests.get(
        COINGECKO_TAO_PRICE_URL,
        params={"ids": "bittensor", "vs_currencies": "usd"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    price = float(payload["bittensor"]["usd"])
    if price <= 0:
        raise ValueError(f"Invalid TAO price received: {price}")
    return price


def dollars_to_tao(amount_usd: float, tao_price_usd: float) -> float:
    if tao_price_usd <= 0:
        raise ValueError("TAO price must be greater than zero.")
    return round(amount_usd / tao_price_usd, 8)