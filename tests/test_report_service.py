"""Report use case: the gates decide whether numbers are published at all.

The fake source carries its own timestamps, so what is tested here is the gate
logic and the refusal behaviour — not the wall clock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeMarketSource

from cartera.adapters.sqlite_store import SqlitePortfolioStore
from cartera.app.services import MarketService, PortfolioService, ReportService
from cartera.config import load_settings
from cartera.domain.errors import SourceUnavailableError
from cartera.domain.models import PortfolioSnapshot, Quote
from cartera.domain.money import Currency

pytestmark = pytest.mark.integration

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def build_settings(tmp_path: Path):
    return load_settings(
        env_file=tmp_path / "nonexistent.env",
        overrides={
            "CARTERA_DATA_DIR": str(tmp_path / "data"),
            "CARTERA_DB_PATH": str(tmp_path / "data" / "cartera.sqlite3"),
        },
    )


def quote(ticker: str, price: str, currency: Currency, age: timedelta) -> Quote:
    return Quote(
        ticker=ticker,
        price=Decimal(price),
        currency=currency,
        as_of=datetime.now(TZ) - age,
        source="fake",
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqlitePortfolioStore]:
    instance = SqlitePortfolioStore(tmp_path / "data" / "cartera.sqlite3")
    yield instance
    instance.close()


def import_portfolio(store: SqlitePortfolioStore, portfolio: PortfolioSnapshot) -> None:
    PortfolioService(store).import_snapshot(portfolio)


async def test_no_snapshot_yields_a_reason_not_a_crash(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([]))

    result = await ReportService(settings, store, market).build()

    assert result.fresh is False
    assert result.summary is None
    assert [issue["code"] for issue in result.issues] == ["no_snapshot"]


async def test_fresh_quotes_publish_numbers(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    portfolio: PortfolioSnapshot,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    settings = build_settings(tmp_path)
    import_portfolio(store, portfolio)
    quotes = [
        quote("GGAL", "5200", Currency.ARS, timedelta(minutes=10)),
        quote("AAPL", "11500", Currency.ARS, timedelta(minutes=10)),
        quote("AL30", "850", Currency.ARS, timedelta(minutes=10)),
        quote("KO", "72", Currency.USD, timedelta(minutes=10)),
    ]
    market = MarketService(settings, source=fake_source(quotes))

    result = await ReportService(settings, store, market).build()

    assert result.fresh is True
    assert result.summary is not None
    # The summary is a JSON-shaped payload (dict[str, object]); the test asserts
    # its shape, so the shape is stated once here rather than at every access.
    valuations = cast("dict[str, dict[str, str]]", result.summary["valuations"])
    ars = valuations["ARS"]
    assert ars["market_value"] == "643500.00"
    assert ars["unrealized_pnl"] == "-770593.00"
    # 0.33% of 520,000 + 0.33% of 115,000 + 0.26% of 8,500
    assert result.liquidation_costs["ARS"] == "2117.60"
    assert result.realized_pnl == {}


async def test_stale_quotes_withhold_the_report(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    portfolio: PortfolioSnapshot,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    settings = build_settings(tmp_path)
    import_portfolio(store, portfolio)
    market = MarketService(
        settings,
        source=fake_source([quote("GGAL", "5200", Currency.ARS, timedelta(days=3))]),
    )

    result = await ReportService(settings, store, market).build()

    assert result.fresh is False
    assert result.summary is None, "a stale run must not publish numbers"
    assert result.issues[0]["code"] == "stale_quote"
    assert result.issues[0]["severity"] == "error"


async def test_unavailable_source_is_reported_as_such(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    portfolio: PortfolioSnapshot,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    settings = build_settings(tmp_path)
    import_portfolio(store, portfolio)
    market = MarketService(
        settings,
        source=fake_source([], error=SourceUnavailableError("fake", "conexión rechazada")),
    )

    result = await ReportService(settings, store, market).build()

    assert result.fresh is False
    assert result.issues[0]["code"] == "source_unavailable"


async def test_unpriced_ticker_is_flagged_but_does_not_block(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    portfolio: PortfolioSnapshot,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    settings = build_settings(tmp_path)
    import_portfolio(store, portfolio)
    market = MarketService(settings, source=fake_source([quote("GGAL", "5200", Currency.ARS, timedelta(minutes=5))]))

    result = await ReportService(settings, store, market).build()

    assert result.fresh is True
    assert result.summary is not None
    unpriced = cast("list[str]", result.summary["unpriced_tickers"])
    assert sorted(unpriced) == ["AAPL", "AL30", "KO"]


async def test_report_records_an_audit_entry(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    portfolio: PortfolioSnapshot,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    settings = build_settings(tmp_path)
    import_portfolio(store, portfolio)
    market = MarketService(settings, source=fake_source([quote("GGAL", "5200", Currency.ARS, timedelta(minutes=5))]))
    before = len(store.audit_entries())

    await ReportService(settings, store, market).build()

    entries = store.audit_entries()
    assert len(entries) == before + 1
    assert entries[-1]["action"] == "report.generate"
    assert store.verify_audit_chain() is True


async def test_source_is_closed_in_the_same_loop(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    portfolio: PortfolioSnapshot,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    """Guards the bug that only appeared in a real CLI run: closing an
    httpx pool from a different event loop than the one that opened it."""
    settings = build_settings(tmp_path)
    import_portfolio(store, portfolio)
    source = fake_source([quote("GGAL", "5200", Currency.ARS, timedelta(minutes=5))])
    market = MarketService(settings, source=source)

    await ReportService(settings, store, market).build()
    await market.aclose()

    assert source.closed is True
