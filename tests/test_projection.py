"""What each projection level discloses, and the refusal that keeps full data home.

The assertions here search for the private strings instead of comparing key sets. A
test that lists the keys it expects passes happily while a ticker sits in a value,
which is the only way this feature can fail quietly.

The payload below is shaped like the real one, private fields and all: per-ticker
weights, quantities, cost basis, an issue whose sentence names an instrument, and a
scoreboard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cartera.adapters.openai_compat import OpenAICompatibleBackend
from cartera.app.analysis import (
    LEVEL_FOR_SCOPE,
    NarrativeService,
    ProjectionLevel,
    Scope,
    backend_for_scope,
    project,
    scope_level,
)
from cartera.config import load_settings
from cartera.domain.errors import CarteraError

TICKERS = ("GGAL", "AAPL", "AL30", "KO")

FIGURES: dict[str, object] = {
    "generated_at": "2026-09-25T15:00:00-03:00",
    "snapshot_as_of": "2026-02-10T11:00:00-03:00",
    "fresh": True,
    "issues": [{"code": "unpriced", "detail": "no price for GGAL", "severity": "warning"}],
    "sources": ["byma-open"],
    "summary": {
        "valuations": {
            "ARS": {
                "market_value": "1485000.00",
                "cost_basis": "1400000.00",
                "unrealized_pnl": "85000.00",
                "cash": "300000.00",
                "total": "1785000.00",
                "weights": {"GGAL": "0.3502", "AAPL": "0.0774", "AL30": "0.5724"},
            }
        },
        "positions": [
            {"ticker": "AL30", "quantity": "1000", "cost_basis": "800000.00", "currency": "ARS", "lots": "1"}
        ],
        "unpriced_tickers": ["GGAL"],
        "priced_positions": "3",
    },
    "realized_pnl": {"ARS": "12000.00"},
    "liquidation_costs": {"ARS": "4305.50"},
    "exposure": {
        "as_of": "2026-09-25T15:00:00-03:00",
        "currencies": [
            {
                "currency": "ARS",
                "market_value": "1485000.00",
                "cash": "300000.00",
                "total": "1785000.00",
                "by_asset_type": [
                    {"label": "bond", "market_value": "850000.00", "weight_pct": "57.24", "instruments": 1},
                    {"label": "equity", "market_value": "520000.00", "weight_pct": "35.02", "instruments": 1},
                ],
                "by_instrument": [
                    {"label": "AL30", "market_value": "850000.00", "weight_pct": "57.24", "instruments": 1},
                    {"label": "GGAL", "market_value": "520000.00", "weight_pct": "35.02", "instruments": 1},
                ],
                "largest": {"label": "AL30", "market_value": "850000.00", "weight_pct": "57.24", "instruments": 1},
                "foreign_pct": "7.74",
                "unpriced": ["GGAL"],
            }
        ],
    },
    "scoreboard": {
        "total_proposals": 4,
        "decided": 2,
        "by_verdict": {"hit": 1, "miss": 1, "pending": 2},
        "hit_rate_pct": "50.00",
        "mean_return_pct": "2.50",
        "best": {"buy": "12.50"},
        "worst": {"sell": "-4.00"},
    },
}


class FakeBackend:
    name = "fake-remote"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.prompts.append(user_prompt)
        return self.answer

    async def aclose(self) -> None:
        pass


def leaked(figures: dict[str, object], *needles: str) -> list[str]:
    text = json.dumps(figures, default=str)
    return [needle for needle in needles if needle in text]


def build_settings(tmp_path: Path, **overrides: str):
    """Settings from an environment that does not exist on disk."""
    return load_settings(env_file=tmp_path / "nonexistent.env", overrides=dict(overrides))


def test_the_full_level_sends_the_figures_untouched() -> None:
    assert project(FIGURES, ProjectionLevel.FULL) is FIGURES


def test_the_aggregated_level_discloses_no_ticker() -> None:
    payload = project(FIGURES, ProjectionLevel.AGGREGATED)
    assert leaked(payload, *TICKERS, "no price for GGAL", "1000", "800000.00") == []


def test_the_aggregated_level_keeps_the_totals_it_was_projected_for() -> None:
    """The analysis still needs the size and the shape, or it has nothing to say."""
    payload = project(FIGURES, ProjectionLevel.AGGREGATED)
    assert leaked(payload, "1485000.00", "1785000.00", "57.24", "bond", "ARS") != []
    assert payload["totals"] == {
        "ARS": {
            "market_value": "1485000.00",
            "unrealized_pnl": "85000.00",
            "cash": "300000.00",
            "total": "1785000.00",
        }
    }


def test_the_aggregated_level_leaves_the_cost_basis_at_home() -> None:
    """Agreed contract: totals, P&L and structure travel; the basis does not."""
    for level in (ProjectionLevel.AGGREGATED, ProjectionLevel.STRUCTURAL):
        payload = project(FIGURES, level)
        assert "cost_basis" not in json.dumps(payload, default=str)


def test_the_structural_level_keeps_the_shape_and_no_amount_at_all() -> None:
    payload = project(FIGURES, ProjectionLevel.STRUCTURAL)
    assert leaked(payload, *TICKERS, "1485000.00", "850000.00", "300000.00", "800000.00") == []
    # The shape survives: which kinds of exposure, how much of the book, how many.
    assert leaked(payload, "bond", "57.24", "7.74") != []


def test_the_structural_level_never_names_an_unpriced_instrument() -> None:
    payload = project(FIGURES, ProjectionLevel.STRUCTURAL)
    books = payload["books"]
    assert isinstance(books, list)
    book = books[0]
    assert isinstance(book, dict)
    assert book["unpriced_count"] == 1
    assert "unpriced" not in book


def test_a_local_scope_sends_everything_and_a_remote_one_does_not() -> None:
    assert LEVEL_FOR_SCOPE == {Scope.LOCAL: ProjectionLevel.FULL, Scope.REMOTE: ProjectionLevel.AGGREGATED}
    assert scope_level(Scope.LOCAL) is ProjectionLevel.FULL
    assert scope_level(Scope.REMOTE) is ProjectionLevel.AGGREGATED
    assert scope_level(Scope.LOCAL, ProjectionLevel.STRUCTURAL) is ProjectionLevel.STRUCTURAL
    assert scope_level(Scope.REMOTE, ProjectionLevel.STRUCTURAL) is ProjectionLevel.STRUCTURAL


def test_asking_the_remote_endpoint_for_the_full_figures_is_refused() -> None:
    """A misconfiguration should be a sentence, not a silent disclosure."""
    with pytest.raises(CarteraError, match="refusing to send the full figures"):
        scope_level(Scope.REMOTE, ProjectionLevel.FULL)


async def test_the_guard_checks_the_prose_against_what_was_sent_not_what_exists() -> None:
    """A figure present in the full data but absent from the projection has no basis."""
    backend = FakeBackend("You hold 1000 units of the largest position, which is 57.24% of the book.")
    service = NarrativeService(backend, "fake")

    result = await service.narrate(FIGURES, scope=Scope.REMOTE, level=ProjectionLevel.STRUCTURAL)

    assert result.verified is False
    assert "1000" in result.unverified_numbers
    assert "57.24" not in result.unverified_numbers, "a weight that was sent can be quoted"
    assert "1000" not in backend.prompts[0], "the quantity must not reach the endpoint"


async def test_the_result_says_which_scope_and_level_were_used() -> None:
    service = NarrativeService(FakeBackend("Nothing to add."), "fake")
    result = await service.narrate(FIGURES, scope=Scope.REMOTE, level=ProjectionLevel.STRUCTURAL)

    assert (result.scope, result.level) == ("remote", "structural")
    assert leaked(result.figures, *TICKERS) == [], "the result carries what was sent, not what exists"


def test_a_remote_backend_needs_its_own_endpoint_and_its_own_secret(tmp_path: Path) -> None:
    configured = build_settings(
        tmp_path,
        CARTERA_ANALYSIS_LLM_BASE_URL="https://remote.example/v1",
        CARTERA_ANALYSIS_LLM_MODEL="big-model",
    )
    remote = backend_for_scope(configured, Scope.REMOTE)
    assert isinstance(remote, OpenAICompatibleBackend)
    assert backend_for_scope(configured, Scope.LOCAL) is None, "the local endpoint is a different setting"
