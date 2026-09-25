"""Reading a holdings table that someone copied out of a broker page.

Pure by design: it takes the text a person pasted and returns a model, with no file
or network access. The page is a private document, but its *shape* is not a secret —
tab-separated rows, Argentine-formatted amounts, a header that states a total — so
the rules for reading it live here rather than in a private add-on.

Three facts come from real pages and shape the code:

* Cells can be empty, and the cash rows are the ones that are: one has no ticker, one
  has no quantity. A positional read that assumes every cell is present shifts the
  whole row by one column and produces a plausible wrong number.
* Fixed income is quoted per 100 nominal value. The code does not guess this from the
  instrument name: it reads the row's own arithmetic, which says which base the page
  used, and reports the contradiction when the two disagree.
* The total in the header is a claim, not a result. It can differ from the sum of the
  rows by a cent, and the difference is reported rather than resolved by adjusting a
  row to make the numbers meet.

The row-arithmetic check is the point of the whole module: run against a page pasted
by a real broker, it catches a factor-of-a-hundred valuation error on the first line
it sees, instead of after it has been multiplied into every indicator.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cartera.domain.metrics import FIXED_INCOME, NOMINAL_BASE, market_value_of
from cartera.domain.models import AssetType, Currency

#: "Posición al 25/09/2026 ARS 65.966.520,19" — the date, the currency and the claim.
HEADER = re.compile(r"Posici[oó]n al\s+(?P<date>\d{2}/\d{2}/\d{4})\s(?P<currency>[A-Z]{3})\s(?P<total>[\d.,]+)")
#: The column titles, which are not data. Matched loosely: the page may hyphenate.
COLUMN_TITLES = ("nombre de la especie", "importe total")
#: Columns in a row: ticker, name, quantity, price, amount. Trailing ones can be empty.
COLUMNS = 5
#: Words that name a currency, and so a cash row, rather than an instrument.
CASH_NAMES = ("PESOS", "DOLAR")
#: A ticker as this market writes one: letters and digits, optionally a US suffix.
TICKER = re.compile(r"^[A-Z0-9.]{2,12}$")
#: Obligaciones negociables end in O (MR35O, YM43O, AERBO) in this market's convention.
CORPORATE_BOND_TICKER = re.compile(r"^[A-Z]{3,5}O$")

CENT = Decimal("0.01")


class QuoteBase(StrEnum):
    """How the page quotes one row: per unit, or per 100 nominal value."""

    PER_UNIT = "per_unit"
    PER_HUNDRED = "per_hundred"


class Verdict(StrEnum):
    """Whether the row's arithmetic agrees with the amount the page printed."""

    #: The printed amount is what the rule gives, to the cent.
    RECONCILED = "reconciled"
    #: Off by a fraction of a cent per unit, which is how a page that converts a USD
    #: price rounds each row. Not an error; not silent either.
    ROUNDED = "rounded"
    #: The printed amount matches the *other* quote base. The page and the instrument
    #: class disagree about how the row is quoted, which is a hundred-fold question.
    CONTRADICTION = "contradiction"
    #: Neither base explains the amount.
    UNRECONCILED = "unreconciled"


class PastedRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    name: str
    #: Units for an equity, nominales for fixed income. The page does not say which,
    #: and the difference matters at the point where the amount is computed.
    quantity: Decimal
    price: Decimal
    #: The amount the page printed, taken as given.
    market_value: Decimal
    asset_type: AssetType
    label: str
    quote_base: QuoteBase
    #: What the row's arithmetic gives, using the same rule the report uses.
    computed_value: Decimal
    verdict: Verdict
    duplicate: bool = False


class PastedCash(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    currency: Currency
    #: The amount in its own currency.
    amount: Decimal
    #: The rate the page used, when the row states one (its USD rows do).
    rate: Decimal | None = None
    ars_value: Decimal | None = None


class PastedHoldings(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: str
    currency: Currency
    #: The total the page claims.
    claimed_total: Decimal
    rows: list[PastedRow] = Field(default_factory=list)
    cash: list[PastedCash] = Field(default_factory=list)
    #: The rate taken from the page's own USD rows, if it has any.
    fx_rate: Decimal | None = None
    warnings: list[str] = Field(default_factory=list)

    @property
    def position_value(self) -> Decimal:
        return sum((row.market_value for row in self.rows), Decimal("0")).quantize(CENT)

    @property
    def computed_position_value(self) -> Decimal:
        return sum((row.computed_value for row in self.rows), Decimal("0")).quantize(CENT)

    @property
    def cash_value(self) -> Decimal:
        in_pesos = [bucket.ars_value for bucket in self.cash if bucket.ars_value is not None]
        return sum(in_pesos, Decimal("0")).quantize(CENT)

    @property
    def computed_total(self) -> Decimal:
        return (self.position_value + self.cash_value).quantize(CENT)

    @property
    def difference(self) -> Decimal:
        """Computed minus claimed. Reported, never reconciled by adjusting a row."""
        return (self.computed_total - self.claimed_total).quantize(CENT)

    def by_class(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for row in self.rows:
            totals[row.label] = totals.get(row.label, Decimal("0")) + row.market_value
        return {label: amount.quantize(CENT) for label, amount in totals.items()}

    def non_reconciling(self) -> list[PastedRow]:
        return [row for row in self.rows if row.verdict in {Verdict.CONTRADICTION, Verdict.UNRECONCILED}]


def _amount(text: str) -> Decimal | None:
    """An Argentine-formatted amount: dot for thousands, comma for decimals."""
    cleaned = text.strip().replace("\u00a0", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _classify(ticker: str, name: str) -> tuple[str, AssetType]:
    """What the instrument is, from the evidence in its own row.

    Traded-elsewhere rows are labelled as such and valued per unit; the fee schedule
    for them is not the local equity one, but this module computes no fees, so the
    difference is not smuggled in here. Options are not classified: the market's
    quotation convention for them has not been verified.
    """
    upper = name.upper()
    if ticker.endswith(".US") or upper.startswith("ADR"):
        return "foreign (traded abroad)", AssetType.EQUITY
    if "CEDEAR" in upper:
        return "cedear", AssetType.CEDEAR
    if upper.startswith(("BONO", "BONCER", "BONTES", "LETRA", "LECAP", "LELIQ")):
        is_bill = upper.startswith(("LETRA", "LECAP", "LELIQ"))
        return ("treasury bill", AssetType.LETER) if is_bill else ("sovereign bond", AssetType.BOND)
    if upper.startswith("ON ") or CORPORATE_BOND_TICKER.match(ticker):
        return "corporate bond (ON)", AssetType.CORP_BOND
    if "CLASE" in upper or "FCI" in upper or "FONDO" in upper:
        return "fund", AssetType.FUND
    return "equity", AssetType.EQUITY


def _verdict(
    asset_type: AssetType, quantity: Decimal, price: Decimal, printed: Decimal
) -> tuple[QuoteBase, Decimal, Verdict]:
    """Which quote base the row used, what it computes to, and whether that agrees."""
    computed = market_value_of(asset_type, quantity, price)
    declared_base = QuoteBase.PER_HUNDRED if asset_type in FIXED_INCOME else QuoteBase.PER_UNIT
    other_base = QuoteBase.PER_UNIT if declared_base is QuoteBase.PER_HUNDRED else QuoteBase.PER_HUNDRED
    alternative = quantity * price / NOMINAL_BASE if other_base is QuoteBase.PER_HUNDRED else quantity * price

    difference = abs(computed - printed)
    # A page that converts a USD price rounds each row, so a few cents per thousand
    # units is arithmetic, not an error. Everything past that is reported.
    tolerance = max(Decimal("0.05"), abs(quantity) * Decimal("0.005"))
    if difference <= CENT:
        return declared_base, computed, Verdict.RECONCILED
    if difference <= tolerance:
        return declared_base, computed, Verdict.ROUNDED
    if abs(alternative - printed) <= tolerance:
        return other_base, alternative, Verdict.CONTRADICTION
    return declared_base, computed, Verdict.UNRECONCILED


def _row_cells(line: str) -> list[str]:
    """A row's cells, padded: the page leaves a cell empty when it has nothing for it."""
    cells = [cell.strip() for cell in line.split("\t")]
    cells += [""] * max(0, COLUMNS - len(cells))
    return cells[:COLUMNS]


def _cash_bucket(
    ticker: str,
    name: str,
    upper_name: str,
    quantity_text: str,
    price_text: str,
    printed: Decimal,
    seen: dict[tuple, int],
) -> PastedCash | None:
    """A cash row as a bucket, or ``None`` when it repeats one already read.

    Cash rows name a currency, and nothing else does. Matching a loose word such as
    "mercado" swallows BYMA — "BOLSAS Y MERCADOS ARG." — as a dollar bucket, which is
    exactly the kind of quietly-wrong read this module exists to prevent.
    """
    key = (ticker, quantity_text, price_text, str(printed))
    if key in seen:
        return None
    seen[key] = 1
    holds_pesos = "PESO" in upper_name
    return PastedCash(
        label=f"{ticker} {name}".strip() or "cash",
        currency=Currency.ARS if holds_pesos else Currency.USD,
        # The peso row states its amount in the amount column; the dollar rows state it
        # in the quantity column, with the rate beside it.
        amount=printed if holds_pesos else (_amount(quantity_text) or Decimal("0")),
        rate=None if holds_pesos else _amount(price_text),
        ars_value=printed,
    )


def parse_holdings(text: str) -> PastedHoldings:
    """Read a pasted holdings table. Raises on text that is not one."""
    header = HEADER.search(text)
    if header is None:
        raise ValueError("this does not look like a holdings page: no 'Posición al <date> <currency> <total>' line")

    day, month, year = header.group("date").split("/")
    as_of = f"{year}-{month}-{day}"
    claimed = _amount(header.group("total"))
    currency = Currency(header.group("currency"))
    warnings: list[str] = []
    if claimed is None:
        raise ValueError("the header total could not be read")

    rows: list[PastedRow] = []
    cash: list[PastedCash] = []
    fx_rate: Decimal | None = None
    seen: dict[tuple, int] = {}

    for line in text.splitlines():
        if not line.strip() or HEADER.search(line):
            continue
        cells = _row_cells(line)
        if any(title in " ".join(cells).lower() for title in COLUMN_TITLES):
            continue
        ticker, name, quantity_text, price_text, amount_text = cells
        printed = _amount(amount_text)
        if printed is None:
            continue  # a legend or a footnote, not a holding

        upper_name = name.upper()
        if upper_name.startswith(CASH_NAMES) or ticker.upper() in {"PESOS", "MEP"}:
            bucket = _cash_bucket(ticker, name, upper_name, quantity_text, price_text, printed, seen)
            if bucket is not None:
                cash.append(bucket)
                if bucket.rate:
                    fx_rate = bucket.rate
            continue

        quantity, price = _amount(quantity_text), _amount(price_text)
        if quantity is None or price is None:
            warnings.append(f"{name or ticker}: a quantity or a price is missing, so nothing can be valued")
            continue

        asset_type = _classify(ticker, name)
        base, computed, verdict = _verdict(asset_type[1], quantity, price, printed)
        key = (ticker, str(quantity), str(price), str(printed))
        duplicate = key in seen
        seen[key] = seen.get(key, 0) + 1
        if duplicate:
            continue  # the page was pasted twice; the row is the same row
        rows.append(
            PastedRow(
                ticker=ticker,
                name=name,
                quantity=quantity,
                price=price,
                market_value=printed,
                asset_type=asset_type[1],
                label=asset_type[0],
                quote_base=base,
                computed_value=computed.quantize(CENT),
                verdict=verdict,
            )
        )

    repeats = [count for count in seen.values() if count > 1]
    if repeats:
        warnings.append(
            f"{len(repeats)} rows appeared more than once: the page looks pasted more than "
            "once, and the repeats were ignored"
        )

    # Built before the model exists: pydantic copies a list field, so appending to the
    # caller's list afterwards would leave the model's own warnings empty.
    warnings.extend(
        f"{row.ticker}: the page printed {row.market_value} but {row.verdict.value} "
        f"with the {row.quote_base.value} base, which gives {row.computed_value}"
        for row in rows
        if row.verdict in {Verdict.CONTRADICTION, Verdict.UNRECONCILED}
    )
    return PastedHoldings(
        as_of=as_of,
        currency=currency,
        claimed_total=claimed,
        rows=rows,
        cash=cash,
        fx_rate=fx_rate,
        warnings=warnings,
    )
