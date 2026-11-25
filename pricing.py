# Copyright 2026 Google LLC / PLE Engineering Harness Contributors
# Licensed under the Apache License, Version 2.0

"""Locked Kaggle Stage 2 model pricing and token-budget accounting."""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True)
class ModelRate:
    """Per-1M-token USD pricing for a Google-sponsored model."""

    input_per_1m: float
    output_per_1m: float
    cached_per_1m: float | None
    hi_input_per_1m: float | None = None
    hi_output_per_1m: float | None = None
    hi_cached_per_1m: float | None = None
    hi_threshold: int = 200_000


# Locked Kaggle Stage 2 table (Topic 761530, 2026-11-24).
LOCKED_RATES: dict[str, ModelRate] = {
    "gemini-4-carbon": ModelRate(4.00, 20.00, 0.20),
    "gemini-4-neon": ModelRate(3.00, 15.00, 0.15),
    "gemini-4-hydrogen": ModelRate(1.50, 7.50, 0.075),
    "gemini-4-argon": ModelRate(4.00, 20.00, 0.20),
    "gemini-3.8-flash": ModelRate(1.50, 7.50, 0.15),
    "gemini-3.1-pro-preview": ModelRate(
        2.00, 12.00, 0.20, hi_input_per_1m=4.00, hi_output_per_1m=18.00, hi_cached_per_1m=0.40
    ),
    "gemma-4-26b": ModelRate(0.15, 0.60, 0.015),
    "claude-opus-5-5": ModelRate(4.00, 20.00, 0.20),
    "gpt-oss-120b": ModelRate(0.09, 0.36, None),
    "glm-5.2": ModelRate(1.40, 4.40, 0.14),
    "llama-4-maverick": ModelRate(0.35, 1.15, None),
}


def resolve_rate(model_name: str) -> ModelRate | None:
    """Finds the locked rate matching `model_name` (bare id or google-cloud/<id>)."""
    bare = model_name.split("/", 1)[-1].strip().lower()
    if bare in LOCKED_RATES:
        return LOCKED_RATES[bare]
    for key, rate in LOCKED_RATES.items():
        if key in bare:
            return rate
    return None


def call_cost_usd(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> float | None:
    """Computes single-call USD cost using the locked Kaggle Stage 2 table.

    `input_tokens` follows OpenAI/Kaggle accounting (total prompt tokens, where
    `cached_tokens` is the cached subset). Non-cached input tokens = max(0, input_tokens - cached_tokens).
    """
    rate = resolve_rate(model_name)
    if rate is None:
        return None
    use_hi = rate.hi_input_per_1m is not None and input_tokens > rate.hi_threshold
    in_p = rate.hi_input_per_1m if use_hi and rate.hi_input_per_1m is not None else rate.input_per_1m
    out_p = rate.hi_output_per_1m if use_hi and rate.hi_output_per_1m is not None else rate.output_per_1m
    cache_p = rate.hi_cached_per_1m if use_hi and rate.hi_cached_per_1m is not None else rate.cached_per_1m

    cached = max(0, min(cached_tokens, input_tokens)) if cache_p is not None else 0
    uncached = max(0, input_tokens - cached)
    cost = (uncached * in_p + output_tokens * out_p) / 1_000_000.0
    if cache_p is not None and cached > 0:
        cost += (cached * cache_p) / 1_000_000.0
    return cost


@dataclasses.dataclass
class UsageTracker:
    """Tracks measured vs. reconstructed token usage and USD spend for one task."""

    model_name: str
    budget_cap_usd: float = 0.0
    calls: int = 0
    measured_calls: int = 0
    reconstructed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    schema_tokens_sent: int = 0
    total_cost_usd: float = 0.0
    cost_basis: str = "measured"

    def record(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        *,
        reconstructed: bool = False,
        schema_tokens: int = 0,
    ) -> float:
        """Records one model completion's tokens and returns incremental USD cost."""
        self.calls += 1
        if reconstructed:
            self.reconstructed_calls += 1
            self.cost_basis = "reconstructed_from_usage"
        else:
            self.measured_calls += 1
        self.input_tokens += max(0, int(input_tokens))
        self.output_tokens += max(0, int(output_tokens))
        self.cached_tokens += max(0, int(cached_tokens))
        self.schema_tokens_sent += max(0, int(schema_tokens))
        delta = call_cost_usd(self.model_name, input_tokens, output_tokens, cached_tokens)
        if delta is None:
            if self.cost_basis == "measured":
                self.cost_basis = "unavailable"
            return 0.0
        self.total_cost_usd += delta
        return delta

    def budget_exhausted(self) -> bool:
        """Returns True if a positive `budget_cap_usd` has been met or exceeded."""
        return self.budget_cap_usd > 0.0 and self.total_cost_usd >= self.budget_cap_usd

    def summary(self) -> dict[str, Any]:
        """Returns non-sensitive accounting metrics safe to log or report."""
        return {
            "model_name": self.model_name,
            "calls": self.calls,
            "measured_calls": self.measured_calls,
            "reconstructed_calls": self.reconstructed_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "schema_tokens_sent": self.schema_tokens_sent,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "budget_cap_usd": self.budget_cap_usd,
            "cost_basis": self.cost_basis,
        }
