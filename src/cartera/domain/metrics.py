"""Deterministic portfolio metrics.

Every number a user sees is produced here, in code, with tests. The analysis
layer may narrate these numbers; it is never allowed to compute them.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from cartera.domain.errors import UnknownTickerError
from cartera.domain.models import AssetType, Lot, PortfolioSnapshot, Position, Quote, Transaction, TxKind
from cartera.domain.money import Currency, apply_pct, as_decimal, money, pct

#: Default fee schedule, expressed as percentages of gross amount. These mirror a
#: public Argentine retail broker schedule and are meant to be overridden per
#: broker through configuration — they are a starting point, not a truth claim.
DEFAULT_COMMISSION_PCT: dict[AssetType, Decimal] = {
    AssetType.EQUITY: Decimal("0.33"),
    AssetType.CEDEAR: Decimal("0.33"),
    AssetType.OPTION: Decimal("0.33"),
    AssetType.BOND: Decimal("0.26"),
    AssetType.CORP_BOND: Decimal("0.26"),
    AssetType.LETER: Decimal("0.26"),
    AssetType.FUND: Decimal("0.00"),
}


class Valuation(BaseModel):
    """Market valuation of everything held in one currency."""

    model_config = ConfigDict(frozen=True)

    currency: Currency
    market_value: Decimal
    cost_basis: Decimal
    unrealized_pnl: Decimal
    cash: Decimal
    total: Decimal
    weights: dict[str, Decimal] = Field(default_factory=dict)
    #: Market value per ticker, before it is turned into weights. Kept here so a
    #: breakdown cannot value the same portfolio a second time and disagree with
    #: the total it is supposed to explain.
    market_values: dict[str, Decimal] = Field(default_factory=dict)

    @property
    def unrealized_pnl_pct(self) -> Decimal | None:
        return pct(self.unrealized_pnl, self.cost_basis)


class PortfolioSummary(BaseModel):
    """The whole deterministic picture for one snapshot plus one set of quotes."""

    model_config = ConfigDict(frozen=True)

    as_of: str
    snapshot_as_of: str
    valuations: dict[Currency, Valuation]
    positions: list[Position]
    priced_positions: int
    unpriced_tickers: list[str]


def commission_for(
    asset_type: AssetType,
    gross: Decimal,
    overrides: dict[AssetType, Decimal] | None = None,
) -> Decimal:
    """Commission for a gross traded amount, quantized to cents."""
    schedule = overrides or DEFAULT_COMMISSION_PCT
    rate = schedule.get(asset_type, DEFAULT_COMMISSION_PCT.get(asset_type, Decimal("0")))
    return apply_pct(gross, rate)


def quote_index(quotes: list[Quote]) -> dict[str, Quote]:
    """Index quotes by ticker. Last one wins, matching input order."""
    return {quote.ticker: quote for quote in quotes}


#: Instruments quoted per 100 nominal value rather than per unit: bonds, corporate
#: bonds (obligaciones negociables) and treasury bills. Verified against the
#: operator's own printed amounts, which divide by a hundred for exactly these and
#: for nothing else in a real portfolio.
FIXED_INCOME = frozenset({AssetType.BOND, AssetType.CORP_BOND, AssetType.LETER})

#: The quote base for fixed income: a price quoted "per 100 nominales".
NOMINAL_BASE = Decimal("100")


def market_value_of(asset_type: AssetType, quantity: Decimal, price: Decimal) -> Decimal:
    """What a position is worth at a quote, in the quote's currency.

    A holding of 5,775 nominales of a bond quoted at 84,130 is worth 4,858,507.50,
    not 485,850,750: fixed income quotes are per 100 nominal value here. The
    distinction costs a factor of a hundred when it is missed, and the wrong number
    looks entirely plausible on screen, which is why it lives in the domain as a
    named rule with tests rather than as an inline multiplication.

    Options are deliberately not classified: their quotation convention has not been
    verified against a real contract, and an unverified convention placed beside a
    verified one is worse than one that is openly missing.
    """
    if asset_type in FIXED_INCOME:
        return quantity * price / NOMINAL_BASE
    return quantity * price


def valuate(
    snapshot: PortfolioSnapshot,
    quotes: list[Quote],
    overrides: dict[AssetType, Decimal] | None = None,
) -> PortfolioSummary:
    """Value the snapshot at the given quotes, currency by currency.

    Positions without a quote are reported as unpriced instead of being valued
    at cost: a silent fallback here would produce a plausible-but-wrong number.
    """
    del overrides  # reserved for fee-aware valuations in a later change
    index = quote_index(quotes)
    positions = snapshot.positions()
    unpriced = sorted({position.ticker for position in positions if position.ticker not in index})
    currencies = sorted({position.currency for position in positions} | set(snapshot.cash))

    valuations: dict[Currency, Valuation] = {}
    for currency in currencies:
        in_currency = [position for position in positions if position.currency is currency]
        cost_basis = money(sum((position.cost_basis for position in in_currency), Decimal("0")))

        values: dict[str, Decimal] = {}
        for position in in_currency:
            quote = index.get(position.ticker)
            if quote is not None:
                values[position.ticker] = money(market_value_of(position.asset_type, position.quantity, quote.price))

        market_value = money(sum(values.values(), Decimal("0")))
        weights = (
            {ticker: (value / market_value).quantize(Decimal("0.0001")) for ticker, value in values.items()}
            if market_value > 0
            else {}
        )
        cash = snapshot.cash_of(currency)
        valuations[currency] = Valuation(
            currency=currency,
            market_value=market_value,
            cost_basis=cost_basis,
            unrealized_pnl=money(market_value - cost_basis),
            cash=cash,
            total=money(market_value + cash),
            weights=weights,
            market_values=values,
        )

    return PortfolioSummary(
        as_of=(max((quote.as_of for quote in quotes), default=snapshot.as_of)).isoformat(),
        snapshot_as_of=snapshot.as_of.isoformat(),
        valuations=valuations,
        positions=positions,
        priced_positions=sum(1 for position in positions if position.ticker in index),
        unpriced_tickers=unpriced,
    )


def realized_pnl(
    transactions: list[Transaction],
    lots: list[Lot],
) -> dict[Currency, Decimal]:
    """Realized P&L from lot-specific sells.

    A sell that references ``lot_id`` is matched against that lot's basis,
    proportionally to the quantity sold. Sells without a ``lot_id`` cannot be
    attributed and are skipped rather than guessed at.
    """
    lots_by_id = {lot.lot_id: lot for lot in lots}
    totals: dict[Currency, Decimal] = {}
    for tx in transactions:
        if tx.kind is not TxKind.SELL or not tx.lot_id or tx.lot_id not in lots_by_id:
            continue
        lot = lots_by_id[tx.lot_id]
        quantity = as_decimal(tx.quantity if tx.quantity is not None else lot.quantity)
        if quantity <= 0:
            continue
        share = min(quantity / lot.quantity, Decimal("1"))
        matched_basis = money(lot.cost_basis * share)
        proceeds = money(tx.amount)
        totals[tx.currency] = totals.get(tx.currency, Decimal("0")) + (proceeds - matched_basis)
    return {currency: money(amount) for currency, amount in totals.items()}


def require_quotes(tickers: list[str], quotes: list[Quote], source: str) -> dict[str, Quote]:
    """Return the quote index, raising when any requested ticker is missing."""
    index = quote_index(quotes)
    missing = [ticker for ticker in tickers if ticker.strip().upper() not in index]
    if missing:
        raise UnknownTickerError(missing, source)
    return index


def liquidation_costs(
    positions: list[Position],
    quotes: list[Quote],
    overrides: dict[AssetType, Decimal] | None = None,
) -> dict[Currency, Decimal]:
    """Estimated commission to exit every priced position, per currency.

    This is a fact about the fee schedule, not a recommendation to sell.
    """
    index = quote_index(quotes)
    totals: dict[Currency, Decimal] = {}
    for position in positions:
        quote = index.get(position.ticker)
        if quote is None:
            continue
        gross = money(market_value_of(position.asset_type, position.quantity, quote.price))
        fee = commission_for(position.asset_type, gross, overrides)
        totals[position.currency] = totals.get(position.currency, Decimal("0")) + fee
    return {currency: money(amount) for currency, amount in totals.items()}
