"""
OpenAI client helpers with built-in model allowlisting and token accounting.

This module intentionally does not depend on a specific OpenAI SDK version.
Instead, it wraps a caller-provided request function and records usage from the
returned response object or metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class OpenAIUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class OpenAIBudgetExceeded(RuntimeError):
    """Raised when a tracked OpenAI request would exceed the token budget."""


class OpenAIModelNotAllowed(RuntimeError):
    """Raised when a request targets a model outside the configured allowlist."""


class TrackedOpenAIClient:
    """
    Small wrapper around an OpenAI-like request function.

    The wrapper enforces:
    - model allowlist
    - cumulative token budget
    - usage extraction from the response
    """

    def __init__(
        self,
        *,
        request_fn: Callable[..., Any],
        allowed_models: tuple[str, ...],
        token_budget: int,
    ) -> None:
        self._request_fn = request_fn
        self.allowed_models = allowed_models
        self.token_budget = int(token_budget)
        self._usage = OpenAIUsage()

    def reset_usage(self) -> None:
        self._usage = OpenAIUsage()

    def usage(self) -> OpenAIUsage:
        return self._usage

    def record_usage(self, *, prompt_tokens: int = 0, completion_tokens: int = 0) -> OpenAIUsage:
        """Manually add token usage to the running session totals."""
        self._usage.prompt_tokens += int(prompt_tokens or 0)
        self._usage.completion_tokens += int(completion_tokens or 0)
        return self._usage

    @property
    def prompt_tokens_used(self) -> int:
        return self._usage.prompt_tokens

    @property
    def completion_tokens_used(self) -> int:
        return self._usage.completion_tokens

    @property
    def total_tokens_used(self) -> int:
        return self._usage.total_tokens

    def remaining_tokens(self) -> int:
        return max(0, self.token_budget - self._usage.total_tokens)

    def available_models(self) -> tuple[str, ...]:
        return self.allowed_models

    def select_model(self, preferred: str | None = None) -> str:
        if preferred and preferred in self.allowed_models:
            return preferred
        if not self.allowed_models:
            raise OpenAIModelNotAllowed("No allowed models configured.")
        return self.allowed_models[0]

    def request(self, *, model: str, endpoint: str = "responses.create", **kwargs: Any) -> Any:
        if model not in self.allowed_models:
            raise OpenAIModelNotAllowed(
                f"Model '{model}' is not allowed. Allowed models: {', '.join(self.allowed_models)}"
            )

        response = self._request_fn(endpoint=endpoint, model=model, **kwargs)
        usage = _extract_usage(response)

        projected_total = self._usage.total_tokens + usage.total_tokens
        if projected_total > self.token_budget:
            raise OpenAIBudgetExceeded(
                f"OpenAI token budget exceeded: {projected_total:,} > {self.token_budget:,}"
            )

        self.record_usage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )
        return response

    def responses_create(self, *, model: str, **kwargs: Any) -> Any:
        return self.request(model=model, endpoint="responses.create", **kwargs)

    def chat_completions_create(self, *, model: str, **kwargs: Any) -> Any:
        return self.request(model=model, endpoint="chat.completions.create", **kwargs)

    def assistants_create(self, *, model: str, **kwargs: Any) -> Any:
        return self.request(model=model, endpoint="assistants.create", **kwargs)

    def threads_create(self, **kwargs: Any) -> Any:
        return self._request_fn(endpoint="threads.create", **kwargs)

    def messages_create(self, *, thread_id: str, **kwargs: Any) -> Any:
        return self._request_fn(endpoint="messages.create", thread_id=thread_id, **kwargs)


def _extract_usage(response: Any) -> OpenAIUsage:
    """Best-effort extraction of token usage from an SDK response object."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")

    if usage is None:
        return OpenAIUsage()

    if isinstance(usage, dict):
        return OpenAIUsage(
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
        )

    return OpenAIUsage(
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )


def create_tracked_openai_client(
    *,
    request_fn: Callable[..., Any],
    allowed_models: tuple[str, ...],
    token_budget: int,
) -> TrackedOpenAIClient:
    """Factory for creating a tracked OpenAI wrapper."""
    return TrackedOpenAIClient(
        request_fn=request_fn,
        allowed_models=allowed_models,
        token_budget=token_budget,
    )


def build_session_usage_report(usage: OpenAIUsage) -> str:
    """Format a human-friendly token usage summary."""
    return (
        f"prompt={usage.prompt_tokens:,}  "
        f"completion={usage.completion_tokens:,}  "
        f"total={usage.total_tokens:,}"
    )