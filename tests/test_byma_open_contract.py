"""Contract tests against the portal's real response shapes.

The two shapes (paginated envelope and bare array) were observed live; if the
portal changes either one, these tests fail before a user sees a wrong number.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from cartera.adapters.byma_open import BASE_URL, UNIVERSES, BymaOpenDataSource
from cartera.domain.errors import SourceUnavailableError
from cartera.domain.models import Settlement
from cartera.domain.money import Currency

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


@respx.mock
async def test_paginated_envelope_is_parsed(client: httpx.AsyncClient) -> None:
    respx.post(f"{BASE_URL}/leading-equity").mock(
        return_value=httpx.Response(200, json=load("blue_chips_page.json")),
    )
    source = BymaOpenDataSource(client)

    quotes = await source.fetch_universe(UNIVERSES["blue-chips"])

    assert [quote.ticker for quote in quotes] == ["GGAL", "ALUA"]
    ggal = quotes[0]
    assert ggal.price == 5200
    assert ggal.currency is Currency.ARS
    assert ggal.settlement is Settlement.CI
    assert ggal.bid == 5190
    assert ggal.ask == 5200
    assert ggal.source == "byma-open-data"
    alua = quotes[1]
    assert alua.settlement is Settlement.T24  # "2" in the vendor payload


@respx.mock
async def test_bare_array_is_parsed_and_zero_prices_dropped(client: httpx.AsyncClient) -> None:
    respx.post(f"{BASE_URL}/cedears").mock(
        return_value=httpx.Response(200, json=load("cedears_page.json")),
    )
    source = BymaOpenDataSource(client)

    quotes = await source.fetch_universe(UNIVERSES["cedears"])

    # KO carries settlementPrice 0 → no trade today → must not become a quote.
    assert [quote.ticker for quote in quotes] == ["AAPL"]


@respx.mock
async def test_pagination_walks_every_page(client: httpx.AsyncClient) -> None:
    page1 = {"content": {"page_number": 1, "page_count": 2}, "data": [{"symbol": "GGAL", "settlementPrice": 100}]}
    page2 = {"content": {"page_number": 2, "page_count": 2}, "data": [{"symbol": "YPFD", "settlementPrice": 200}]}
    route = respx.post(f"{BASE_URL}/public-bonds").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)],
    )
    source = BymaOpenDataSource(client)

    quotes = await source.fetch_universe(UNIVERSES["public-bonds"])

    assert route.call_count == 2
    assert sorted(quote.ticker for quote in quotes) == ["GGAL", "YPFD"]


@respx.mock
async def test_request_uses_post_with_json_content_type(client: httpx.AsyncClient) -> None:
    route = respx.post(f"{BASE_URL}/cedears").mock(return_value=httpx.Response(200, json=[]))
    source = BymaOpenDataSource(client)

    await source.fetch_universe(UNIVERSES["cedears"])

    request = route.calls[0].request
    assert request.method == "POST"
    assert request.headers["content-type"] == "application/json"
    # The portal rejects GET with 405, so this is a behaviour worth pinning.
    assert request.headers["origin"] == "https://open.bymadata.com.ar"


@respx.mock
async def test_filtering_by_ticker_and_settlement(client: httpx.AsyncClient) -> None:
    respx.post(f"{BASE_URL}/leading-equity").mock(
        return_value=httpx.Response(200, json=load("blue_chips_page.json")),
    )
    source = BymaOpenDataSource(client)

    only_ggal = await source.fetch_quotes(["ggal"], [UNIVERSES["blue-chips"]], (Settlement.CI,))
    assert [quote.ticker for quote in only_ggal] == ["GGAL"]

    only_t24 = await source.fetch_quotes([], [UNIVERSES["blue-chips"]], (Settlement.T24,))
    assert [quote.ticker for quote in only_t24] == ["ALUA"]


@respx.mock
async def test_server_error_becomes_source_unavailable(client: httpx.AsyncClient) -> None:
    respx.post(f"{BASE_URL}/cedears").mock(return_value=httpx.Response(503, text="upstream down"))
    source = BymaOpenDataSource(client, max_attempts=1)

    with pytest.raises(SourceUnavailableError) as excinfo:
        await source.fetch_universe(UNIVERSES["cedears"])
    assert excinfo.value.source == "byma-open-data"


@respx.mock
async def test_transport_failure_becomes_source_unavailable(client: httpx.AsyncClient) -> None:
    respx.post(f"{BASE_URL}/cedears").mock(side_effect=httpx.ConnectError("sin ruta"))
    source = BymaOpenDataSource(client, max_attempts=1)

    with pytest.raises(SourceUnavailableError):
        await source.fetch_universe(UNIVERSES["cedears"])


@respx.mock
async def test_market_session_is_parsed(client: httpx.AsyncClient) -> None:
    respx.post(f"{BASE_URL}/market-time").mock(
        return_value=httpx.Response(
            200,
            json={
                "isWorkingDay": True,
                "marketClosingTime": "17:00:00",
                "timezone": "GMT-03:00",
                "marketOpeningTime": "10:30:00",
            },
        ),
    )
    session = await BymaOpenDataSource(client).fetch_session()

    assert session.is_working_day is True
    assert session.opens_at is not None, "a working day must publish an opening time"
    assert session.closes_at is not None, "a working day must publish a closing time"
    assert session.opens_at.isoformat() == "10:30:00"
    assert session.closes_at.isoformat() == "17:00:00"


def test_quote_timestamp_is_dated_in_market_timezone(client: httpx.AsyncClient) -> None:
    source = BymaOpenDataSource(client)
    moment = source._as_of("14:48:10")

    assert moment.tzinfo is not None
    assert (moment.hour, moment.minute) == (14, 48)
