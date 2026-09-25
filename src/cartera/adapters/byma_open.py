"""Market-data adapter for the exchange's public open-data portal.

Verified contract (checked live, 2026-09-25):
``POST /vanoms-be-core/rest/api/bymadata/free/{endpoint}`` with a JSON body.
The portal answers two different shapes — ``{"content": {...}, "data": [...]}``
for the paginated universes and a bare array for others — so both are handled
here and covered by contract tests.

No credentials are involved. The feed is the delayed (free) one, so quotes carry
their source and timestamp and are subject to the freshness gate in
``cartera.domain.rules``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from datetime import time as clock_time
from typing import Any

import httpx

from cartera.domain.errors import SourceUnavailableError
from cartera.domain.models import AssetType, Quote, Settlement
from cartera.domain.money import Currency, as_decimal
from cartera.ports.market_data import MarketSession, Universe

BASE_URL = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free"
SOURCE_NAME = "byma-open-data"

#: From this status up, a retry is worth attempting; below it, the request is
#: malformed or refused and retrying would only repeat the mistake.
SERVER_ERROR_MIN = 500

UNIVERSES: dict[str, Universe] = {
    "blue-chips": Universe(key="leading-equity", asset_type=AssetType.EQUITY),
    "general-equity": Universe(key="general-equity", asset_type=AssetType.EQUITY),
    "cedears": Universe(key="cedears", asset_type=AssetType.CEDEAR),
    "public-bonds": Universe(key="public-bonds", asset_type=AssetType.BOND),
    "corp-bonds": Universe(key="negociable-obligations", asset_type=AssetType.CORP_BOND),
    "options": Universe(key="options", asset_type=AssetType.OPTION),
}

#: Universes that answer a bare JSON array instead of the paginated envelope.
BARE_ARRAY_UNIVERSES = {"options"}


class BymaOpenDataSource:
    """Read-only access to the public, delayed market feed."""

    name = SOURCE_NAME

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout_seconds: float = 20.0,
        page_size: int = 200,
        max_pages: int = 30,
        max_attempts: int = 2,
        timezone: str = "America/Argentina/Buenos_Aires",
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_attempts = max_attempts
        self._timezone = timezone

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- HTTP -----------------------------------------------------------------

    async def _post(self, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.post(
                    f"{BASE_URL}/{endpoint}",
                    json=payload or {},
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://open.bymadata.com.ar",
                        "Referer": "https://open.bymadata.com.ar/",
                        "Accept": "application/json",
                    },
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                if status < SERVER_ERROR_MIN or attempt == self._max_attempts:
                    raise SourceUnavailableError(SOURCE_NAME, f"{endpoint} answered HTTP {status}") from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    raise SourceUnavailableError(SOURCE_NAME, f"{endpoint} failed: {exc}") from exc
            else:
                return response.json()
            await asyncio.sleep(0.3 * attempt)
        raise SourceUnavailableError(SOURCE_NAME, f"{endpoint} failed: {last_error}")

    # -- Parsing --------------------------------------------------------------

    @staticmethod
    def _records(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return ``(records, page_info)`` for either response shape."""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)], {}
        if isinstance(payload, dict):
            data = payload.get("data")
            records = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
            page = payload.get("content")
            return records, page if isinstance(page, dict) else {}
        return [], {}

    def _to_quote(self, record: dict[str, Any]) -> Quote | None:
        ticker = str(record.get("symbol") or "").strip().upper()
        if not ticker:
            return None
        price = record.get("settlementPrice") or record.get("trade") or record.get("closingPrice")
        if price in (None, 0, 0.0, ""):
            return None
        currency_code = str(record.get("denominationCcy") or "ARS").strip().upper()
        try:
            currency = Currency(currency_code)
        except ValueError:
            currency = Currency.ARS
        return Quote(
            ticker=ticker,
            price=as_decimal(price),
            currency=currency,
            as_of=self._as_of(record.get("tradeHour")),
            source=SOURCE_NAME,
            settlement=Settlement.from_code(record.get("settlementType")),
            bid=self._optional(record.get("bidPrice")),
            ask=self._optional(record.get("offerPrice")),
        )

    @staticmethod
    def _optional(value: Any) -> Any:
        return None if value in (None, "", 0, 0.0) else as_decimal(value)

    def _as_of(self, trade_hour: Any) -> datetime:
        """Quotes report a clock time only, so date it in the market timezone."""
        from zoneinfo import ZoneInfo

        tzinfo = ZoneInfo(self._timezone)
        now = datetime.now(tzinfo)
        if isinstance(trade_hour, str) and ":" in trade_hour:
            try:
                hour, minute, *rest = (int(part) for part in trade_hour.split(":"))
                second = rest[0] if rest else 0
                local = datetime.combine(now.date(), clock_time(hour, minute, second), tzinfo=tzinfo)
            except (ValueError, TypeError):
                return now
            # A clock time still ahead of us means the row belongs to the previous session.
            return local if local <= now else local - timedelta(days=1)
        return now

    # -- Public API -----------------------------------------------------------

    async def fetch_session(self) -> MarketSession:
        payload = await self._post("market-time")
        if not isinstance(payload, dict):
            raise SourceUnavailableError(SOURCE_NAME, "market-time returned an unexpected payload")
        return MarketSession(
            is_working_day=bool(payload.get("isWorkingDay")),
            opens_at=self._parse_clock(payload.get("marketOpeningTime")),
            closes_at=self._parse_clock(payload.get("marketClosingTime")),
            timezone=str(payload.get("timezone") or self._timezone),
        )

    @staticmethod
    def _parse_clock(value: Any) -> clock_time | None:
        if not isinstance(value, str) or ":" not in value:
            return None
        try:
            hour, minute, *rest = (int(part) for part in value.split(":"))
            return clock_time(hour, minute, rest[0] if rest else 0)
        except (ValueError, TypeError):
            return None

    async def fetch_universe(self, universe: Universe) -> list[Quote]:
        quotes: list[Quote] = []
        if universe.key in BARE_ARRAY_UNIVERSES:
            records, _ = self._records(await self._post(universe.key))
            return [quote for record in records if (quote := self._to_quote(record))]

        page_number = 1
        page_count = 1
        while page_number <= min(page_count, self._max_pages):
            records, page = self._records(
                await self._post(universe.key, {"page_number": page_number, "page_size": self._page_size}),
            )
            quotes.extend(quote for record in records if (quote := self._to_quote(record)))
            page_count = int(page.get("page_count") or 1)
            page_number += 1
        return quotes

    async def fetch_quotes(
        self,
        tickers: list[str],
        universes: list[Universe],
        settlements: tuple[Settlement, ...] = (Settlement.CI,),
    ) -> list[Quote]:
        wanted = {ticker.strip().upper() for ticker in tickers if ticker.strip()}
        selected = universes or [universe for universe in UNIVERSES.values() if universe.asset_type]
        collected: list[Quote] = []
        for universe in selected:
            for quote in await self.fetch_universe(universe):
                if wanted and quote.ticker not in wanted:
                    continue
                if quote.settlement is not None and quote.settlement not in settlements:
                    continue
                collected.append(quote)
        return collected


def default_universes() -> list[Universe]:
    """The universes queried when the caller does not specify any."""
    return [UNIVERSES["blue-chips"], UNIVERSES["cedears"], UNIVERSES["public-bonds"]]
