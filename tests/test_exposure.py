"""The exposure breakdown, verified against figures that can be checked by hand.

The fixture portfolio is deliberately small: two currencies, four positions and a
cedear, with round prices, so every expected percentage here was computed on paper
and not by running the code and copying what it said.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from conftest import TZ

from cartera.domain.exposure import ExposureReport, exposure_report
from cartera.domain.metrics import valuate
from cartera.domain.models import PortfolioSnapshot
from cartera.domain.money import Currency


def build(portfolio: PortfolioSnapshot, quotes: list) -> ExposureReport:
    return exposure_report(valuate(portfolio, quotes), portfolio)


def test_every_priced_instrument_lands_in_exactly_one_group(portfolio: PortfolioSnapshot, quotes: list) -> None:
    """No double counting, and nothing priced left out of the breakdown."""
    report = build(portfolio, quotes)
    ars = next(book for book in report.currencies if book.currency is Currency.ARS)

    assert len(ars.by_instrument) == 3
    assert {group.label for group in ars.by_instrument} == {"GGAL", "AAPL", "AL30"}
    # 520000 + 115000 + 850000 = 1485000, the same market value the report published.
    assert sum(group.market_value for group in ars.by_instrument) == ars.market_value


def test_the_type_breakdown_accounts_for_the_whole_book(portfolio: PortfolioSnapshot, quotes: list) -> None:
    ars = next(book for book in build(portfolio, quotes).currencies if book.currency is Currency.ARS)

    by_type = {group.label: group for group in ars.by_asset_type}
    # The bond leg is quoted per 100 nominal value: 1,000 nominales at 850 is 8,500,
    # not 850,000. See test_fixed_income.py for the operator-verified cases.
    assert by_type["bond"].market_value == Decimal("8500.00")
    assert by_type["cedear"].market_value == Decimal("115000.00")
    assert by_type["equity"].market_value == Decimal("520000.00")
    assert by_type["bond"].instruments == 1
    assert sum(group.market_value for group in ars.by_asset_type) == ars.market_value


def test_weights_are_percentages_and_not_fractions(portfolio: PortfolioSnapshot, quotes: list) -> None:
    """0.8081 and 80.81 are the same number to a machine and different ones to a reader."""
    ars = next(book for book in build(portfolio, quotes).currencies if book.currency is Currency.ARS)

    weights = {group.label: group.weight_pct for group in ars.by_instrument}
    assert weights["GGAL"] == Decimal("80.81")  # 520000 / 643500
    assert weights["AAPL"] == Decimal("17.87")  # 115000 / 643500
    assert weights["AL30"] == Decimal("1.32")  # 8500 / 643500
    assert sum(value for value in weights.values() if value is not None) == Decimal("100.00")


def test_the_largest_holding_is_the_biggest_one_by_value(portfolio: PortfolioSnapshot, quotes: list) -> None:
    ars = next(book for book in build(portfolio, quotes).currencies if book.currency is Currency.ARS)
    assert ars.largest is not None
    assert ars.largest.label == "GGAL"
    assert ars.largest.weight_pct == Decimal("80.81")


def test_a_cedear_is_reported_as_foreign_exposure(portfolio: PortfolioSnapshot, quotes: list) -> None:
    """A cedear is foreign equity in a local wrapper; the ticker alone hides that."""
    ars = next(book for book in build(portfolio, quotes).currencies if book.currency is Currency.ARS)
    assert ars.foreign_pct == Decimal("17.87")


def test_a_usd_equity_is_not_counted_as_a_cedear(portfolio: PortfolioSnapshot, quotes: list) -> None:
    usd = next(book for book in build(portfolio, quotes).currencies if book.currency is Currency.USD)
    assert usd.foreign_pct is None
    assert usd.by_asset_type[0].label == "equity"


def test_currencies_are_never_consolidated(portfolio: PortfolioSnapshot, quotes: list) -> None:
    """Two books, never one: a single total would need an FX rate this view does not have."""
    report = build(portfolio, quotes)

    assert [book.currency for book in report.currencies] == [Currency.ARS, Currency.USD]
    usd = next(book for book in report.currencies if book.currency is Currency.USD)
    assert usd.market_value == Decimal("360.00")  # 5 x 72
    assert usd.cash == Decimal("50")
    assert usd.total == Decimal("410.00")
    assert usd.by_instrument[0].weight_pct == Decimal("100.00")


def test_an_unpriced_position_is_named_and_left_out_of_the_percentages(
    portfolio: PortfolioSnapshot,
    quotes: list,
) -> None:
    """A share computed from a missing price is a wrong number in the right format."""
    without_al30 = [quote for quote in quotes if quote.ticker != "AL30"]
    ars = next(book for book in build(portfolio, without_al30).currencies if book.currency is Currency.ARS)

    assert ars.unpriced == ["AL30"]
    assert "bond" not in {group.label for group in ars.by_asset_type}
    assert {group.label: group.weight_pct for group in ars.by_instrument} == {
        "GGAL": Decimal("81.89"),  # 520000 / 635000
        "AAPL": Decimal("18.11"),  # 115000 / 635000
    }
    assert ars.market_value == Decimal("635000.00")


def test_an_empty_portfolio_produces_no_groups_rather_than_a_zero(quotes: list) -> None:
    """Nothing owned is not the same as everything worth zero."""
    empty = PortfolioSnapshot(as_of=datetime(2026, 9, 25, 15, 0, tzinfo=TZ), source="test")
    report = build(empty, quotes)

    assert report.currencies == []
    assert report.as_of
