"""Error hierarchy. Every failure the user can act on has a specific type."""

from __future__ import annotations


class CarteraError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(CarteraError):
    """Configuration is missing, contradictory or malformed."""


class MoneyError(CarteraError, ValueError):
    """An amount could not be interpreted as an exact decimal."""


class DomainError(CarteraError):
    """A business rule was violated."""


class InvalidLotError(DomainError):
    """A lot is internally inconsistent (negative quantity, bad fees...)."""


class UnknownLotError(DomainError):
    """A sell referenced a lot that does not exist."""


class MarketDataError(CarteraError):
    """Base class for market-data failures."""


class SourceUnavailableError(MarketDataError):
    """A market-data source did not answer.

    Kept separate from staleness on purpose: "the source is down" and "the data
    is old" need different responses from the caller.
    """

    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"market-data source {source!r} unavailable: {detail}")
        self.source = source
        self.detail = detail


class UnknownTickerError(MarketDataError):
    """A requested ticker is absent from the returned dataset."""

    def __init__(self, tickers: list[str], source: str) -> None:
        super().__init__(f"tickers not found in {source!r}: {', '.join(sorted(tickers))}")
        self.tickers = sorted(tickers)
        self.source = source


class StaleQuoteError(MarketDataError):
    """A quote is older than the configured tolerance."""

    def __init__(self, ticker: str, age_seconds: float, max_age_seconds: float) -> None:
        super().__init__(
            f"quote for {ticker} is {age_seconds:.0f}s old, "
            f"tolerance is {max_age_seconds:.0f}s",
        )
        self.ticker = ticker
        self.age_seconds = age_seconds
        self.max_age_seconds = max_age_seconds


class StoreError(CarteraError):
    """Storage-level failure."""


class DuplicateTransactionError(StoreError):
    """The same transaction id was appended twice."""
