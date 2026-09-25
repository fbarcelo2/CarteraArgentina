"""Money primitives: exactness, rounding and refusal of bad input."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cartera.domain.errors import MoneyError
from cartera.domain.money import apply_pct, as_decimal, money, pct, ratio

pytestmark = pytest.mark.unit


def test_float_is_converted_without_binary_artefacts() -> None:
    assert as_decimal(0.33) == Decimal("0.33")
    assert as_decimal(0.1) + as_decimal(0.2) == Decimal("0.3")


def test_string_and_int_are_exact() -> None:
    assert as_decimal("839.00") == Decimal("839")
    assert as_decimal(7) == Decimal(7)


def test_boolean_is_rejected() -> None:
    with pytest.raises(MoneyError):
        as_decimal(True)


def test_garbage_string_is_rejected() -> None:
    with pytest.raises(MoneyError):
        as_decimal("no-es-un-numero")


def test_money_rounds_half_up_to_cents() -> None:
    assert money("1.005") == Decimal("1.01")
    assert money("1.004") == Decimal("1.00")


def test_apply_pct_matches_expected_commission() -> None:
    # 0.33% of a 500,000 gross trade is 1,650.00
    assert apply_pct(Decimal("500000"), Decimal("0.33")) == Decimal("1650.00")


def test_ratio_and_pct_are_safe_on_zero_denominator() -> None:
    assert ratio(Decimal("10"), Decimal("0")) is None
    assert pct(Decimal("10"), Decimal("0")) is None
    assert pct(Decimal("25"), Decimal("200")) == Decimal("12.5")
