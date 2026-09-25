"""Reading a real pasted holdings page, and the trap it has to catch.

The paste below is a genuine page with the instruments and amounts kept: 34 rows, 31
of them holdings, in seven classes, with the empty cells that make a positional read
go wrong. The expectations are the page's own numbers, so the test fails if the code
and the operator ever disagree.

The last two tests are the reason the module exists: a page that quotes fixed income
per unit (the hundred-fold error) has to be reported as a contradiction, and a page
pasted twice has to be read once.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cartera.domain.holdings import QuoteBase, Verdict, parse_holdings
from cartera.domain.models import AssetType, Currency

#: A real page: tab-separated, with the cash rows missing a cell each.
PASTE = "\n".join(
    [
        " Posición al 25/09/2026 ARS 65.966.520,19",
        "Ticker\tNombre de la Especie\tCantidad\tPrecio\tImporte Total",
        "PAMP\tPAMPA HOLDING\t441,00\t5.060,00\t2.231.460,00",
        "TGSU2\tTGSU2 - TRANS. DE GAS DEL SUR\t162,00\t8.640,00\t1.399.680,00",
        "YPFD\tYPF S.A.\t2.000,00\t8.440,00\t16.880.000,00",
        "KO.US\tTHE COCA COLA COMPANY\t6,00\t124.042,26\t744.253,56",
        "YPF.US\tYPF SOCIEDAD ANONIMA ADR\t5,00\t76.189,62\t380.948,10",
        "ETHA\tCEDEAR ISHARES ETHEREUM TR ETF\t350,00\t6.580,00\t2.303.000,00",
        "BYMA\tBOLSAS Y MERCADOS ARG. $ ORD.\t3.000,00\t249,75\t749.250,00",
        "LOMA\tACC.ORD. LOMA NEGRA S.A. 1 VOTO $ ESC.\t244,00\t3.012,50\t735.050,00",
        "BRK.B.US\tBERKSHIRE HATHAWAY INC CL B NEW\t2,00\t742.033,13\t1.484.066,25",
        "LOMA.US\tADR LOMA NEGRA CORP. (LOMA)\t4,00\t17.204,60\t68.818,38",
        "DOCU.US\tDOCUSIGN INC\t1,00\t78.999,15\t78.999,15",
        "BBAR.US\tADR BANCO BBVA ARGENTINA S.A\t1,00\t29.590,70\t29.590,70",
        "ETHE.US\tGRAYSCALE ETHEREUM TR ETH (ETHE)\t7,00\t23.140,86\t161.986,02",
        "JEPQ.US\tNasdaq Equity Premium Income ETF\t2,00\t88.500,20\t177.000,39",
        "BITX.US\t2x Bitcoin Strategy ETF (BITX)\t8,00\t18.986,99\t151.895,88",
        "BULL.US\tWEBULL CORP (BULL)\t6,00\t11.615,75\t69.694,47",
        "AL30\tBONO REP. ARGENTINA USD STEP UP 2030\t5.775,00\t84.130,00\t4.858.507,50",
        "TX26\tBONO DEL TESORO BONCER 2% $ 2026\t31.072,00\t748,30\t232.511,78",
        "AL29\tBONO REP ARGENTINA USD 1% 2029\t1.000,00\t82.650,00\t826.500,00",
        "TZXD6\tBONTES $ A DESC AJ CER V15/12/26\t729.570,00\t307,40\t2.242.698,18",
        "GD41\tBONOS REP. ARG. U$S STEP UP V.09/07/41\t500,00\t107.810,00\t539.050,00",
        "MR35O\tON GMCTR CL.35 V28/8/27\t1.967,00\t33.400,00\t656.978,00",
        "AERBO\tON AEROP ARG 2000 11 V15/12/26 U$S CG\t91,00\t158.410,00\t144.153,10",
        "YM39O\tON YPF CLASE 39 VTO 22/07/30 U$S CG\t1.000,00\t169.630,00\t1.696.300,00",
        "MGCQO\tON PAMPA ENERGIA CL.25 V06/08/28 U$S CG\t200,00\t165.030,00\t330.060,00",
        "YM43O\tON YPF S. A. CL.43 VTO. 14/04/30 USD\t400,00\t155.990,00\t623.960,00",
        "ALLARTA\tALLARIA DOLAR RETORNO TOTAL Clase A\t771,10\t1.994,22\t1.537.742,26",
        "KO\tCOCA COLA COMPANY CEDEAR\t486,00\t28.500,00\t13.851.000,00",
        "AZN\tCEDEAR AZTRAZDEN\t11,00\t67.400,00\t741.400,00",
        "BBD\tCEDEAR BANCO BRADESCO S.A.\t28,00\t5.525,00\t154.700,00",
        "AMZN\tCEDEAR AMAZON.COM, INC\t130,00\t2.820,00\t366.600,00",
        "Pesos\tPESOS\t\t1,00\t9.496.643,39",
        "\tDOLARES MERC. VALORES\t9,54\t1.510,50\t14.410,17",
        "MEP\tDOLAR MEP.\t5,04\t1.510,50\t7.612,92",
    ]
)


def row(ticker: str):
    return next(row for row in parse_holdings(PASTE).rows if row.ticker == ticker)


# -- the header and the totals ------------------------------------------------


def test_the_header_gives_the_date_the_currency_and_the_claim() -> None:
    holdings = parse_holdings(PASTE)

    assert holdings.as_of == "2026-09-25"
    assert holdings.currency is Currency.ARS
    assert holdings.claimed_total == Decimal("65.966.520,19".replace(".", "").replace(",", "."))


def test_the_sum_of_the_rows_disagrees_with_the_header_by_one_cent() -> None:
    """The page rounds its own total; the difference is reported, not absorbed."""
    holdings = parse_holdings(PASTE)

    assert len(holdings.rows) == 31
    assert holdings.computed_total == Decimal("65966520.20")
    assert holdings.difference == Decimal("0.01")


def test_every_holding_row_reconciles_against_its_own_arithmetic() -> None:
    holdings = parse_holdings(PASTE)

    assert holdings.non_reconciling() == []
    assert holdings.warnings == [], "a page that adds up should raise nothing"


def test_the_cash_rows_are_read_as_cash_and_not_as_positions() -> None:
    holdings = parse_holdings(PASTE)

    usd = sum((bucket.amount for bucket in holdings.cash if bucket.currency is Currency.USD), Decimal("0"))
    assert usd == Decimal("14.58")
    assert next(bucket for bucket in holdings.cash if bucket.currency is Currency.ARS).amount == Decimal("9496643.39")
    assert holdings.cash_value == Decimal("9518666.48")


def test_the_rate_comes_from_the_page_itself() -> None:
    assert parse_holdings(PASTE).fx_rate == Decimal("1510.50")


# -- fixed income, quoted per 100 nominales ----------------------------------


def test_a_bond_row_is_read_as_per_hundred() -> None:
    al30 = row("AL30")

    assert al30.asset_type is AssetType.BOND
    assert al30.quote_base is QuoteBase.PER_HUNDRED
    assert al30.verdict is Verdict.RECONCILED
    assert al30.computed_value == Decimal("4858507.50")
    assert al30.quantity == Decimal("5775.00"), "the quantity is nominales, not units"


def test_the_corporate_bonds_and_the_bill_follow_the_same_base() -> None:
    for ticker in ("MR35O", "AERBO", "YM39O", "MGCQO", "YM43O", "TZXD6"):
        assert row(ticker).quote_base is QuoteBase.PER_HUNDRED, ticker


def test_everything_else_is_read_per_unit() -> None:
    for ticker in ("YPFD", "PAMP", "KO", "KO.US", "ALLARTA", "BYMA"):
        assert row(ticker).quote_base is QuoteBase.PER_UNIT, ticker


def test_the_classes_add_up_to_what_the_page_shows() -> None:
    by_class = parse_holdings(PASTE).by_class()

    assert by_class["equity"] == Decimal("21995440.00")
    assert by_class["cedear"] == Decimal("17416700.00")
    assert by_class["sovereign bond"] == Decimal("8699267.46")
    assert by_class["corporate bond (ON)"] == Decimal("3451451.10")
    assert by_class["foreign (traded abroad)"] == Decimal("3347252.90")
    assert by_class["fund"] == Decimal("1537742.26")
    # The classes cover the holdings; the cash sits beside them (14.4% of the page).
    assert sum(by_class.values()) == parse_holdings(PASTE).position_value == Decimal("56447853.72")


def test_a_cedear_and_the_foreign_line_are_not_the_same_class() -> None:
    """KO is held twice: once as a cedear and once traded abroad, and they differ."""
    assert row("KO").label == "cedear"
    assert row("KO.US").label == "foreign (traded abroad)"


# -- the traps the module exists for -----------------------------------------


def test_a_page_that_quotes_fixed_income_per_unit_is_reported_as_a_contradiction() -> None:
    """The hundred-fold error: the amount says per unit, the instrument says per hundred."""
    wrong = PASTE.replace("5.775,00\t84.130,00\t4.858.507,50", "5.775,00\t84.130,00\t485.850.750,00")
    holdings = parse_holdings(wrong)
    al30 = next(row for row in holdings.rows if row.ticker == "AL30")

    assert al30.verdict is Verdict.CONTRADICTION
    assert al30.quote_base is QuoteBase.PER_UNIT
    assert al30 in holdings.non_reconciling()
    assert any("AL30" in warning for warning in holdings.warnings)


def test_a_page_that_does_not_add_up_at_all_is_unreconciled() -> None:
    wrong = PASTE.replace("5.775,00\t84.130,00\t4.858.507,50", "5.775,00\t84.130,00\t999.999,99")
    al30 = next(row for row in parse_holdings(wrong).rows if row.ticker == "AL30")

    assert al30.verdict is Verdict.UNRECONCILED


def test_a_row_that_is_fractionally_off_is_rounded_and_not_an_error() -> None:
    """Converting a USD price rounds the row: BITX is 4 cents off, which is not a bug."""
    assert row("BITX.US").verdict is Verdict.ROUNDED


def test_a_page_pasted_twice_is_read_once() -> None:
    twice = PASTE + "\n" + PASTE
    holdings = parse_holdings(twice)

    assert len(holdings.rows) == 31
    assert holdings.computed_total == Decimal("65966520.20")
    assert any("more than once" in warning for warning in holdings.warnings)


def test_text_that_is_not_a_holdings_page_is_refused() -> None:
    with pytest.raises(ValueError, match="does not look like a holdings page"):
        parse_holdings("just some notes about the market")
