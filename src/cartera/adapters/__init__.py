"""Concrete adapters: market data, FX and storage.

Adapters are the only place allowed to talk to the outside world.
"""

from cartera.adapters.byma_open import UNIVERSES, BymaOpenDataSource, default_universes
from cartera.adapters.fx_dolarapi import DolarApiSource, FxRate
from cartera.adapters.sqlite_store import SqlitePortfolioStore

__all__ = [
    "UNIVERSES",
    "BymaOpenDataSource",
    "DolarApiSource",
    "FxRate",
    "SqlitePortfolioStore",
    "default_universes",
]
