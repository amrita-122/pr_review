from decimal import Decimal

from app.review.cost import Prices, Usage, estimate_cost, usage_from_metadata

PRICES = Prices(Decimal("2"), Decimal("10"))


def test_estimate_cost_uses_per_million_prices() -> None:
    assert estimate_cost(Usage(1_000_000, 100_000), PRICES) == Decimal("3")
    assert estimate_cost(Usage(), PRICES) == Decimal(0)


def test_usage_is_summed_across_models() -> None:
    meta = {
        "m1": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "m2": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
    }
    assert usage_from_metadata(meta) == Usage(11, 7)
