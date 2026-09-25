"""Exact decimal money primitives.

Rule: a binary float never touches a monetary value. Floats coming from JSON are
converted through their shortest round-trip representation so that ``0.33``
stays ``0.33`` and not ``0.33000000000000002``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum

from cartera.domain.errors import MoneyError

Number = int | str | Decimal | float

CENT = Decimal("0.01")
PERCENT = Decimal("100")


class Currency(StrEnum):
    """Currencies this project accounts in."""

    ARS = "ARS"
    USD = "USD"


def as_decimal(value: Number) -> Decimal:
    """Return ``value`` as an exact Decimal, refusing nonsense input."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise MoneyError("a boolean is not an amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise MoneyError(f"not a decimal number: {value!r}") from exc
    if isinstance(value, float):
        return Decimal(repr(value))
    raise MoneyError(f"unsupported amount type: {type(value).__name__}")


def money(amount: Number) -> Decimal:
    """Quantize a monetary amount to cents, rounding half up."""
    return as_decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)


def ratio(numerator: Number, denominator: Number) -> Decimal | None:
    """Safe division: ``None`` when the denominator is zero."""
    den = as_decimal(denominator)
    if den == 0:
        return None
    return as_decimal(numerator) / den


def pct(part: Number, whole: Number) -> Decimal | None:
    """``part`` as a percentage of ``whole`` (0-100 scale), or ``None``."""
    value = ratio(part, whole)
    return None if value is None else value * PERCENT


def apply_pct(amount: Number, percent: Number) -> Decimal:
    """Apply a percentage (``0.33`` means 0.33%) and quantize to cents."""
    return money(as_decimal(amount) * as_decimal(percent) / PERCENT)
