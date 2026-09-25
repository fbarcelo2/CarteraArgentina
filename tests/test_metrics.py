"""Portfolio metrics: every expected figure is verifiable by hand.

Fixture (see conftest): GGAL 100 @ 5000 + 1650 fees, AAPL 10 @ 11000 + 363,
AL30 1000 @ 800 + 2080, KO 5 @ 70. Cash 300,000 ARS and 50 USD.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from cartera.domain.errors import UnknownTickerError
from cartera.domain.metrics import commission_for, realized_pnl, require_quotes, valuate
from cartera.domain.models import AssetType, PortfolioSnapshot, Quote, Transaction
from cartera.domain.money import Currency
from cartera.domain.rules import check_freshness

pytestmark = pytest.mark.unit

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def test_cost_basis_includes_fees(portfolio: PortfolioSnapshot) -> None:
    lots = {lot.lot_id: lot for lot in portfolio.lots}
    assert lots["lot-ggal-1"].cost_basis == Decimal("501650.00")
    assert lots["lot-aapl-1"].cost_basis == Decimal("110363.00")
    assert lots["lot-al30-1"].cost_basis == Decimal("802080.00")


def test_positions_keep_lot_granularity(portfolio: PortfolioSnapshot) -> None:
    positions = portfolio.positions()
    assert [position.ticker for position in positions] == ["AAPL", "AL30", "GGAL", "KO"]
    ggal = next(position for position in positions if position.ticker == "GGAL")
    assert ggal.lot_count == 1
    assert ggal.cost_basis == Decimal("501650.00")


def test_valuation_per_currency(portfolio: PortfolioSnapshot, quotes: list[Quote]) -> None:
    summary = valuate(portfolio, quotes)

    ars = summary.valuations[Currency.ARS]
    assert ars.market_value == Decimal("643500.00")
    assert ars.cost_basis == Decimal("1414093.00")
    assert ars.unrealized_pnl == Decimal("-770593.00")
    assert ars.cash == Decimal("300000.00")
    assert ars.total == Decimal("943500.00")

    usd = summary.valuations[Currency.USD]
    assert usd.market_value == Decimal("360.00")
    assert usd.cost_basis == Decimal("350.00")
    assert usd.unrealized_pnl == Decimal("10.00")
    assert usd.total == Decimal("410.00")


def test_weights_sum_to_one(portfolio: PortfolioSnapshot, quotes: list[Quote]) -> None:
    ars = valuate(portfolio, quotes).valuations[Currency.ARS]
    assert ars.weights == {"GGAL": Decimal("0.8081"), "AAPL": Decimal("0.1787"), "AL30": Decimal("0.0132")}
    assert sum(ars.weights.values()) == Decimal("1.0000")


def test_unpriced_position_is_reported_not_guessed(portfolio: PortfolioSnapshot, quotes: list[Quote]) -> None:
    without_al30 = [quote for quote in quotes if quote.ticker != "AL30"]
    summary = valuate(portfolio, without_al30)

    assert summary.unpriced_tickers == ["AL30"]
    assert summary.priced_positions == 3
    # AL30 must not be valued at cost: it is simply absent from market value.
    assert summary.valuations[Currency.ARS].market_value == Decimal("635000.00")


def test_realized_pnl_uses_the_referenced_lot(
    portfolio: PortfolioSnapshot,
    sell_transaction: Transaction,
) -> None:
    # Half of a lot whose basis is 501,650 → 250,825 matched; proceeds 260,000.
    totals = realized_pnl([sell_transaction], portfolio.lots)
    assert totals[Currency.ARS] == Decimal("9175.00")


def test_sell_without_lot_reference_is_not_attributed(
    portfolio: PortfolioSnapshot,
    sell_transaction: Transaction,
) -> None:
    orphan = sell_transaction.model_copy(update={"lot_id": None})
    assert realized_pnl([orphan], portfolio.lots) == {}


def test_commission_schedule_defaults_and_override() -> None:
    assert commission_for(AssetType.EQUITY, Decimal("500000")) == Decimal("1650.00")
    assert commission_for(AssetType.BOND, Decimal("1000000")) == Decimal("2600.00")
    override = {AssetType.BOND: Decimal("0.10")}
    assert commission_for(AssetType.BOND, Decimal("1000000"), override) == Decimal("1000.00")


def test_missing_ticker_raises_instead_of_silently_valuing(portfolio: PortfolioSnapshot) -> None:
    now = datetime(2026, 9, 25, 15, 0, tzinfo=TZ)
    quotes = [Quote(ticker="GGAL", price=Decimal("5200"), currency=Currency.ARS, as_of=now, source="t")]
    with pytest.raises(UnknownTickerError) as excinfo:
        require_quotes(["GGAL", "AL30"], quotes, source="test")
    assert excinfo.value.tickers == ["AL30"]


def test_future_quote_is_detected_by_rules(portfolio: PortfolioSnapshot) -> None:
    now = datetime(2026, 9, 25, 15, 0, tzinfo=TZ)
    ahead = now + timedelta(minutes=30)
    quotes = [Quote(ticker="GGAL", price=Decimal("1"), currency=Currency.ARS, as_of=ahead, source="t")]
    report = check_freshness(quotes, now)
    assert not report.ok
    assert report.errors[0].code == "future_quote"
