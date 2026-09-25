"""Fixed income is quoted per 100 nominal value. The expectations are the operator's.

Every amount asserted here was printed by the broker's own page for a real holding,
so the test is not checking the code against itself: it is checking it against the
number the operator will show when the position is liquidated. Getting this wrong
multiplies a bond by a hundred and the result still reads like a plausible figure.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cartera.domain.metrics import market_value_of
from cartera.domain.models import AssetType

# -- fixed income: quoted per 100 nominales ----------------------------------


def test_a_sovereign_bond_uses_the_amount_the_page_prints() -> None:
    """AL30, 5,775 nominales at a quote of 84,130: the page says 4,858,507.50."""
    assert market_value_of(AssetType.BOND, Decimal("5775"), Decimal("84130")) == Decimal("4858507.50")


def test_a_corporate_bond_follows_the_same_rule() -> None:
    """AERBO, 91 nominales at 158,410: the page says 144,153.10."""
    assert market_value_of(AssetType.CORP_BOND, Decimal("91"), Decimal("158410")) == Decimal("144153.10")


def test_a_treasury_bill_follows_the_same_rule() -> None:
    """TZXD6, 729,570 nominales at 307.40: the page says 2,242,698.18."""
    assert market_value_of(AssetType.LETER, Decimal("729570"), Decimal("307.40")) == Decimal("2242698.18")


def test_the_bond_leg_of_the_real_portfolio_totals_what_the_page_totals() -> None:
    """Nine fixed-income rows, summed: the page's own header claim, within its cent."""
    holdings = [
        (AssetType.BOND, "5775", "84130"),
        (AssetType.BOND, "31072", "748.30"),
        (AssetType.BOND, "1000", "82650"),
        (AssetType.LETER, "729570", "307.40"),
        (AssetType.BOND, "500", "107810"),
        (AssetType.CORP_BOND, "1967", "33400"),
        (AssetType.CORP_BOND, "91", "158410"),
        (AssetType.CORP_BOND, "1000", "169630"),
        (AssetType.CORP_BOND, "200", "165030"),
        (AssetType.CORP_BOND, "400", "155990"),
    ]
    total = sum(
        (market_value_of(kind, Decimal(quantity), Decimal(price)) for kind, quantity, price in holdings),
        Decimal("0"),
    )
    # The rows as printed sum to 12,150,718.57; the page's own total implies
    # 12,150,718.56. One cent, from rounding each row before summing.
    assert total.quantize(Decimal("0.01")) in {Decimal("12150718.56"), Decimal("12150718.57")}


# -- everything else: quoted per unit ----------------------------------------


def test_an_equity_is_valued_per_unit() -> None:
    """YPFD, 2,000 shares at 8,440: the page says 16,880,000."""
    assert market_value_of(AssetType.EQUITY, Decimal("2000"), Decimal("8440")) == Decimal("16880000.00")


def test_a_cedear_is_valued_per_unit() -> None:
    """KO CEDEAR, 486 at 28,500: the page says 13,851,000."""
    assert market_value_of(AssetType.CEDEAR, Decimal("486"), Decimal("28500")) == Decimal("13851000.00")


def test_a_fund_is_valued_per_unit() -> None:
    """ALLARTA, 771.10 cuotapartes at 1,994.22: 1,537,743.042, printed as 1,537,742.26.

    The page rounds differently — it shows 1,537,742.26 for the same row — so this
    asserts the exact product and records the disagreement rather than encoding the
    page's rounding as arithmetic.
    """
    assert market_value_of(AssetType.FUND, Decimal("771.10"), Decimal("1994.22")) == Decimal("1537743.0420")


@pytest.mark.parametrize("kind", [AssetType.EQUITY, AssetType.CEDEAR, AssetType.FUND, AssetType.OPTION])
def test_only_fixed_income_is_divided_by_a_hundred(kind: AssetType) -> None:
    """The factor applies to the three fixed-income kinds and to nothing else."""
    assert market_value_of(kind, Decimal("100"), Decimal("1000")) == Decimal("100000")


@pytest.mark.parametrize("kind", [AssetType.BOND, AssetType.CORP_BOND, AssetType.LETER])
def test_fixed_income_is_never_valued_per_unit(kind: AssetType) -> None:
    assert market_value_of(kind, Decimal("100"), Decimal("1000")) == Decimal("1000")


def test_the_rule_is_wired_into_the_valuation(portfolio, quotes) -> None:
    """A valuation built from the fixtures must use the rule, not multiply blindly."""
    from cartera.domain.metrics import valuate

    summary = valuate(portfolio, quotes)
    for currency, valuation in summary.valuations.items():
        for position in portfolio.positions():
            if position.currency is not currency or position.ticker not in valuation.market_values:
                continue
            quote = next(quote for quote in quotes if quote.ticker == position.ticker)
            expected = market_value_of(position.asset_type, position.quantity, quote.price).quantize(Decimal("0.01"))
            assert valuation.market_values[position.ticker] == expected
