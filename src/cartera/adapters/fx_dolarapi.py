"""Reference-rate adapter (FX).

Rates are not instruments: they are kept in their own type so they can never be
mistaken for a tradeable position or sneak into P&L arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from cartera.domain.errors import SourceUnavailableError
from cartera.domain.money import money

BASE_URL = "https://dolarapi.com/v1/dolares"
SOURCE_NAME = "dolarapi"


@dataclass(frozen=True)
class FxRate:
    """A reference rate for one FX market ("oficial", "mep", "blue"...)."""

    market: str
    label: str
    buy: Decimal | None
    sell: Decimal | None
    as_of: datetime
    source: str = SOURCE_NAME

    @property
    def mid(self) -> Decimal | None:
        if self.buy is None or self.sell is None:
            return None
        return money((self.buy + self.sell) / 2)


class DolarApiSource:
    """Free reference rates. No credentials, no account."""

    name = SOURCE_NAME

    def __init__(self, client: httpx.AsyncClient | None = None, *, timeout_seconds: float = 15.0) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_rates(self) -> list[FxRate]:
        try:
            response = await self._client.get(BASE_URL, headers={"Accept": "application/json"})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(SOURCE_NAME, str(exc)) from exc
        payload: Any = response.json()
        if not isinstance(payload, list):
            raise SourceUnavailableError(SOURCE_NAME, "unexpected payload shape")
        rates: list[FxRate] = []
        for record in payload:
            if not isinstance(record, dict):
                continue
            rates.append(
                FxRate(
                    market=str(record.get("casa") or "unknown"),
                    label=str(record.get("nombre") or ""),
                    buy=self._decimal(record.get("compra")),
                    sell=self._decimal(record.get("venta")),
                    as_of=self._as_of(record.get("fechaActualizacion")),
                ),
            )
        return rates

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        return None if value in (None, "") else money(value)

    @staticmethod
    def _as_of(value: Any) -> datetime:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.now().astimezone()
