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
class PriceTable:
    """Prices by model name; a model not listed is priced at `default` (the review model)."""

    default: Prices
    by_model: Mapping[str, Prices]

    def for_model(self, model: str) -> Prices:
        return self.by_model.get(model, self.default)

    @classmethod
    def of(cls, prices: "Prices | PriceTable") -> "PriceTable":
        return prices if isinstance(prices, PriceTable) else cls(default=prices, by_model={})


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


def cost_from_metadata(by_model: Mapping[str, Any], prices: PriceTable) -> tuple[Usage, Decimal]:
    """Total usage and its estimated cost, pricing each model's tokens at that model's rate."""
    total = Decimal(0)
    for model, counts in by_model.items():
        usage = Usage(int(counts.get("input_tokens", 0)), int(counts.get("output_tokens", 0)))
        total += estimate_cost(usage, prices.for_model(model))
    return usage_from_metadata(by_model), total
