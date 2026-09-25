"""What the portfolio is exposed to, grouped, without valuing it a second time.

Two limits are deliberate, and both exist to avoid a number that looks right and is
not.

Nothing here crosses currencies. Adding ARS and USD needs an exchange rate with its
own provenance, its own timestamp and its own failure mode, so this view reports one
currency at a time rather than inventing a consolidated total.

Nothing here re-prices anything. The market values come from the same valuation the
report already published, so a breakdown cannot disagree with the total above it —
the classic way an exposure view loses its reader's trust permanently.

Only priced instruments contribute to a percentage. An unpriced position is listed
by name and left out of the arithmetic: a share of the book computed from a missing
price is a wrong number wearing a correct one's clothes.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field

from cartera.domain.metrics import PortfolioSummary
from cartera.domain.models import AssetType, Currency, PortfolioSnapshot
from cartera.domain.money import money, pct

#: Weights are read, not audited to the eighteenth decimal. ``pct`` divides and
#: stops there, so an unquantized weight reaches a screen as 35.0168350168350168.
PERCENT_QUANTUM = Decimal("0.01")


def _weight_pct(part: Decimal, whole: Decimal) -> Decimal | None:
    """A share of the book in percent (0-100 scale), rounded to two decimals."""
    value = pct(part, whole)
    return None if value is None else value.quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP)


class ExposureGroup(BaseModel):
    """One row of a breakdown: a group of instruments and what it weighs."""

    model_config = ConfigDict(frozen=True)

    label: str
    market_value: Decimal
    #: Share of the market value in that currency, or ``None`` when there is none.
    weight_pct: Decimal | None = None
    instruments: int = 0


class CurrencyExposure(BaseModel):
    """Exposure in one currency, which is the only unit this view totals."""

    model_config = ConfigDict(frozen=True)

    currency: Currency
    market_value: Decimal
    cash: Decimal
    total: Decimal
    by_asset_type: list[ExposureGroup] = Field(default_factory=list)
    by_instrument: list[ExposureGroup] = Field(default_factory=list)
    largest: ExposureGroup | None = None
    #: Share held through cedears: foreign equity in a local wrapper. Worth its own
    #: number because it is a currency-and-country exposure wearing a local ticker.
    foreign_pct: Decimal | None = None
    unpriced: list[str] = Field(default_factory=list)


class ExposureReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: str
    currencies: list[CurrencyExposure] = Field(default_factory=list)


def exposure_report(summary: PortfolioSummary, snapshot: PortfolioSnapshot) -> ExposureReport:
    """Group the already-valued positions by asset type and by instrument.

    Pure: no clock, no I/O, no second valuation. ``snapshot`` is taken because the
    asset type of an instrument lives on its position, and positions are what the
    valuation was built from.
    """
    del snapshot  # positions travel inside the summary; the parameter documents intent
    asset_types = {position.ticker: position.asset_type for position in summary.positions}
    currencies = {position.ticker: position.currency for position in summary.positions}

    exposures: list[CurrencyExposure] = []
    for currency, valuation in sorted(summary.valuations.items(), key=lambda item: item[0].value):
        priced = valuation.market_values
        by_type: dict[AssetType, list[Decimal]] = {}
        for ticker, value in priced.items():
            # Every priced ticker came from a position, so the lookup cannot miss.
            by_type.setdefault(asset_types[ticker], []).append(value)

        by_asset_type = [
            ExposureGroup(
                label=asset_type.value,
                market_value=money(sum(values, Decimal("0"))),
                weight_pct=_weight_pct(money(sum(values, Decimal("0"))), valuation.market_value),
                instruments=len(values),
            )
            for asset_type, values in by_type.items()
        ]
        by_asset_type.sort(key=lambda group: group.market_value, reverse=True)

        by_instrument = [
            ExposureGroup(
                label=ticker,
                market_value=money(value),
                weight_pct=_weight_pct(money(value), valuation.market_value),
                instruments=1,
            )
            for ticker, value in priced.items()
        ]
        by_instrument.sort(key=lambda group: group.market_value, reverse=True)

        foreign = money(
            sum(
                (value for ticker, value in priced.items() if asset_types[ticker] is AssetType.CEDEAR),
                Decimal("0"),
            )
        )
        exposures.append(
            CurrencyExposure(
                currency=currency,
                market_value=valuation.market_value,
                cash=valuation.cash,
                total=valuation.total,
                by_asset_type=by_asset_type,
                by_instrument=by_instrument,
                largest=by_instrument[0] if by_instrument else None,
                foreign_pct=_weight_pct(foreign, valuation.market_value) if foreign > 0 else None,
                unpriced=sorted(ticker for ticker in summary.unpriced_tickers if currencies.get(ticker) is currency),
            )
        )

    return ExposureReport(as_of=summary.as_of, currencies=exposures)
