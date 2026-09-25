"""Domain models: instruments, lots, transactions, positions, portfolio state.

Everything here is inert data plus derived arithmetic. No network, no database,
no clock access beyond values passed in — that is what makes the numbers
reproducible and testable.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cartera.domain.errors import InvalidLotError
from cartera.domain.money import Currency, as_decimal, money


class AssetType(StrEnum):
    """Instrument families with distinct fee schedules."""

    EQUITY = "equity"
    CEDEAR = "cedear"
    BOND = "bond"
    CORP_BOND = "corp_bond"
    LETER = "leter"
    OPTION = "option"
    FUND = "fund"


class Settlement(StrEnum):
    """Argentine settlement windows. Values match the market vendor's codes."""

    CI = "1"
    T24 = "2"
    T48 = "3"

    @classmethod
    def from_code(cls, code: str | None) -> Settlement | None:
        """Accept both the numeric vendor code and the common textual labels."""
        if code is None:
            return None
        normalized = str(code).strip().upper()
        aliases: dict[str, Settlement] = {
            "CI": cls.CI,
            "24HS": cls.T24,
            "T24": cls.T24,
            "48HS": cls.T48,
            "T48": cls.T48,
        }
        if normalized in aliases:
            return aliases[normalized]
        by_value: dict[str, Settlement] = {member.value: member for member in cls}
        return by_value.get(normalized)

    @property
    def label(self) -> str:
        return {"1": "CI", "2": "24HS", "3": "48HS"}[self.value]


class TxKind(StrEnum):
    """Ledger entry kinds. Amounts are expressed as signed cash movements."""

    BUY = "buy"
    SELL = "sell"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    DIVIDEND = "dividend"
    FEE = "fee"
    ADJUSTMENT = "adjustment"


class Quote(BaseModel):
    """A price observation, carrying where and when it came from."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    price: Decimal
    currency: Currency
    as_of: datetime
    source: str
    settlement: Settlement | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None

    @field_validator("ticker")
    @classmethod
    def _clean_ticker(cls, value: str) -> str:
        return value.strip().upper()

    def age_seconds(self, now: datetime) -> float:
        return (now - self.as_of).total_seconds()


class Lot(BaseModel):
    """A single purchase, tracked individually so sells can target it.

    Lot-level accounting is what allows choosing which purchase to liquidate
    first for tax purposes; it only works if a lot is never merged away.
    """

    model_config = ConfigDict(frozen=True)

    lot_id: str
    ticker: str
    asset_type: AssetType
    quantity: Decimal
    unit_price: Decimal
    currency: Currency
    opened_at: datetime
    settlement: Settlement = Settlement.CI
    fees: Decimal = Decimal("0")
    source: str = "snapshot"

    @field_validator("ticker")
    @classmethod
    def _clean_ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("quantity", mode="before")
    @classmethod
    def _positive_quantity(cls, value: object) -> Decimal:
        quantity = as_decimal(value)  # type: ignore[arg-type]
        if quantity <= 0:
            raise InvalidLotError("lot quantity must be greater than zero")
        return quantity

    @field_validator("unit_price", "fees", mode="before")
    @classmethod
    def _decimal(cls, value: object) -> Decimal:
        return as_decimal(value)  # type: ignore[arg-type]

    @property
    def gross_cost(self) -> Decimal:
        return money(self.quantity * self.unit_price)

    @property
    def cost_basis(self) -> Decimal:
        """Total acquisition cost including fees."""
        return money(self.quantity * self.unit_price + self.fees)

    @property
    def avg_price(self) -> Decimal:
        """Per-unit cost including fees, quantized to six places."""
        return (self.cost_basis / self.quantity).quantize(Decimal("0.000001"))


class Transaction(BaseModel):
    """An immutable ledger row. ``amount`` is the cash effect, signed."""

    model_config = ConfigDict(frozen=True)

    tx_id: str
    kind: TxKind
    occurred_at: datetime
    currency: Currency
    amount: Decimal
    ticker: str | None = None
    asset_type: AssetType | None = None
    lot_id: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    fees: Decimal = Decimal("0")
    note: str | None = None

    @field_validator("amount", "fees", mode="before")
    @classmethod
    def _decimal(cls, value: object) -> Decimal:
        return as_decimal(value)  # type: ignore[arg-type]


class Position(BaseModel):
    """Open exposure in one instrument, derived from its lots."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    asset_type: AssetType
    currency: Currency
    quantity: Decimal
    cost_basis: Decimal
    lot_count: int = Field(ge=1)

    @property
    def avg_price(self) -> Decimal:
        return (self.cost_basis / self.quantity).quantize(Decimal("0.000001"))


class PortfolioSnapshot(BaseModel):
    """Point-in-time portfolio: cash per currency plus the open lots."""

    as_of: datetime
    cash: dict[Currency, Decimal] = Field(default_factory=dict)
    lots: list[Lot] = Field(default_factory=list)
    source: str = "manual"
    label: str | None = None

    def cash_of(self, currency: Currency) -> Decimal:
        return money(self.cash.get(currency, Decimal("0")))

    def positions(self) -> list[Position]:
        """Aggregate lots into positions; lot granularity stays in ``lots``."""
        buckets: dict[tuple[str, Currency], list[Lot]] = {}
        for lot in self.lots:
            buckets.setdefault((lot.ticker, lot.currency), []).append(lot)
        positions = [
            Position(
                ticker=ticker,
                asset_type=lots[0].asset_type,
                currency=currency,
                quantity=sum((lot.quantity for lot in lots), Decimal("0")),
                cost_basis=money(sum((lot.cost_basis for lot in lots), Decimal("0"))),
                lot_count=len(lots),
            )
            for (ticker, currency), lots in buckets.items()
        ]
        return sorted(positions, key=lambda position: position.ticker)

    def lot_ids(self) -> list[str]:
        return [lot.lot_id for lot in self.lots]
