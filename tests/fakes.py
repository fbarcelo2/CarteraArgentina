"""Test doubles. Kept out of the package: users should not depend on them."""

from __future__ import annotations

from datetime import time

from cartera.domain.models import Quote, Settlement
from cartera.ports.market_data import MarketSession


class FakeMarketSource:
    """In-memory market source implementing ``cartera.ports.MarketDataSource``.

    Use cases need a source whose timestamps the test controls: that is the only
    way to exercise the freshness gate deterministically, without waiting on the
    wall clock or on a live endpoint.
    """

    name = "fake"

    def __init__(
        self,
        quotes: list[Quote] | None = None,
        error: Exception | None = None,
        session: MarketSession | None = None,
    ) -> None:
        self.quotes: list[Quote] = quotes or []
        self.error = error
        self.session = session or MarketSession(
            is_working_day=True,
            opens_at=time(10, 30),
            closes_at=time(17, 0),
            timezone="America/Argentina/Buenos_Aires",
        )
        self.closed = False

    async def fetch_universe(self, universe: object) -> list[Quote]:
        if self.error is not None:
            raise self.error
        return list(self.quotes)

    async def fetch_quotes(
        self,
        tickers: list[str],
        universes: list[object],
        settlements: tuple[Settlement, ...] = (),
    ) -> list[Quote]:
        if self.error is not None:
            raise self.error
        wanted = {ticker.strip().upper() for ticker in tickers if ticker.strip()}
        return [quote for quote in self.quotes if not wanted or quote.ticker in wanted]

    async def fetch_session(self) -> MarketSession:
        if self.error is not None:
            raise self.error
        return self.session

    async def aclose(self) -> None:
        self.closed = True
