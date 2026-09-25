"""Ports: the contracts the outside world must satisfy.

Adapters implement these; the domain and use cases only depend on them. This is
what keeps a new market-data source or a new storage backend from touching
business rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Protocol, runtime_checkable

from cartera.domain.models import AssetType, Quote, Settlement


@dataclass(frozen=True)
class MarketSession:
    """Whether the market trades today and during which hours."""

    is_working_day: bool
    opens_at: time | None
    closes_at: time | None
    timezone: str


@dataclass(frozen=True)
class Universe:
    """A named set of instruments a source can return in one call."""

    key: str
    asset_type: AssetType | None


@runtime_checkable
class MarketDataSource(Protocol):
    """Read-only market data. Implementations must never place orders."""

    name: str

    async def fetch_universe(self, universe: Universe) -> list[Quote]:
        """Every quote the source has for ``universe``."""
        ...

    async def fetch_quotes(
        self,
        tickers: list[str],
        universes: list[Universe],
        settlements: tuple[Settlement, ...],
    ) -> list[Quote]:
        """Quotes for the requested tickers, missing ones omitted."""
        ...

    async def fetch_session(self) -> MarketSession:
        """Market calendar information for the current day."""
        ...

    async def aclose(self) -> None:
        """Release network resources."""
        ...
