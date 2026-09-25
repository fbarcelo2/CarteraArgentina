"""Shared test fixtures: a small, hand-checked portfolio."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeMarketSource

from cartera.domain.models import (
    AssetType,
    Lot,
    PortfolioSnapshot,
    Quote,
    Settlement,
    Transaction,
    TxKind,
)
from cartera.domain.money import Currency

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 25, 15, 0, tzinfo=TZ)


@pytest.fixture
def portfolio() -> PortfolioSnapshot:
    """Three positions across two currencies plus cash in both.

    Figures are chosen so every expected value can be verified by hand.
    """
    opened = datetime(2026, 2, 10, 11, 0, tzinfo=TZ)
    return PortfolioSnapshot(
        as_of=opened,
        source="test",
        cash={Currency.ARS: Decimal("300000"), Currency.USD: Decimal("50")},
        lots=[
            Lot(
                lot_id="lot-ggal-1",
                ticker="GGAL",
                asset_type=AssetType.EQUITY,
                quantity=Decimal("100"),
                unit_price=Decimal("5000"),
                fees=Decimal("1650"),
                currency=Currency.ARS,
                opened_at=opened,
            ),
            Lot(
                lot_id="lot-aapl-1",
                ticker="AAPL",
                asset_type=AssetType.CEDEAR,
                quantity=Decimal("10"),
                unit_price=Decimal("11000"),
                fees=Decimal("363"),
                currency=Currency.ARS,
                opened_at=opened,
                settlement=Settlement.T24,
            ),
            Lot(
                lot_id="lot-al30-1",
                ticker="AL30",
                asset_type=AssetType.BOND,
                quantity=Decimal("1000"),
                unit_price=Decimal("800"),
                fees=Decimal("2080"),
                currency=Currency.ARS,
                opened_at=opened,
            ),
            Lot(
                lot_id="lot-ko-usd",
                ticker="KO",
                asset_type=AssetType.EQUITY,
                quantity=Decimal("5"),
                unit_price=Decimal("70"),
                fees=Decimal("0"),
                currency=Currency.USD,
                opened_at=opened,
            ),
        ],
    )


@pytest.fixture
def quotes(now: datetime) -> list[Quote]:
    fresh = now - timedelta(minutes=20)
    return [
        Quote(ticker="GGAL", price=Decimal("5200"), currency=Currency.ARS, as_of=fresh, source="test"),
        Quote(ticker="AAPL", price=Decimal("11500"), currency=Currency.ARS, as_of=fresh, source="test"),
        Quote(ticker="AL30", price=Decimal("850"), currency=Currency.ARS, as_of=fresh, source="test"),
        Quote(ticker="KO", price=Decimal("72"), currency=Currency.USD, as_of=fresh, source="test"),
    ]


@pytest.fixture
def sell_transaction() -> Transaction:
    """Half of the GGAL lot sold, referencing the lot explicitly."""
    return Transaction(
        tx_id="tx-sell-ggal",
        kind=TxKind.SELL,
        occurred_at=datetime(2026, 9, 20, 12, 0, tzinfo=TZ),
        currency=Currency.ARS,
        amount=Decimal("260000"),
        ticker="GGAL",
        asset_type=AssetType.EQUITY,
        lot_id="lot-ggal-1",
        quantity=Decimal("50"),
        price=Decimal("5200"),
    )


@pytest.fixture
def fake_source() -> Callable[..., FakeMarketSource]:
    def build(
        quotes: list[Quote] | None = None,
        error: Exception | None = None,
    ) -> FakeMarketSource:
        return FakeMarketSource(quotes=quotes, error=error)

    return build
