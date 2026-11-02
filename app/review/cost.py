"""Token usage -> estimated USD. Pure: no model, no IO."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Prices:
    input_per_mtok: Decimal
    output_per_mtok: Decimal


@dataclass(frozen=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


def usage_from_metadata(by_model: Mapping[str, Any]) -> Usage:
    """Sum LangChain's UsageMetadataCallbackHandler.usage_metadata (model name -> counts)."""
    tokens_in = sum(int(u.get("input_tokens", 0)) for u in by_model.values())
    tokens_out = sum(int(u.get("output_tokens", 0)) for u in by_model.values())
    return Usage(tokens_in, tokens_out)


def estimate_cost(usage: Usage, prices: Prices) -> Decimal:
    """Cache discounts are ignored, so this is a slight over-estimate."""
    return (
        Decimal(usage.tokens_in) * prices.input_per_mtok
        + Decimal(usage.tokens_out) * prices.output_per_mtok
    ) / _MILLION
