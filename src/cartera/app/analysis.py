"""Narration over figures computed elsewhere, with a guard against invention.

The model's only job here is prose. If its answer contains a number that was not
in the input, the answer is reported as unverified and the offending tokens are
named, because a narrative that quietly invents a figure is worse than no
narrative at all: it launders a guess into something that reads like the output of
a calculator.

The guard is a lint, not a proof. Number formatting is ambiguous across locales —
``6.280`` is six thousand two hundred and eighty here and six point two eight
elsewhere — so it normalises by digits and accepts a match in several forms. What
it cannot trace, it names, and the caller decides.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from cartera.adapters.openai_compat import OpenAICompatibleBackend
from cartera.app.services import ReportResult
from cartera.config import Settings
from cartera.domain.proposals import Scoreboard
from cartera.ports.store import AnalysisBackend
from cartera.security.secrets import get_secret

#: Numbers as a reader writes them: 1.234,56 · 1234.56 · 6.280 · 12 · -4.5.
NUMBER = re.compile(r"-?\d+(?:[.,]\d+)*")

# The name of the keyring entry that holds the key, not a password: what bandit
# looks for cannot be written literally here, which is the point.
API_KEY_SECRET = "CARTERA_LLM_API_KEY"  # nosec B105

SYSTEM_PROMPT = (
    "You write a short, plain-language note about a portfolio, for its owner. "
    "You are given figures that were already computed by tested code. "
    "Rules, in order of importance: "
    "1. NEVER compute, estimate, extrapolate or round a figure. Every number you "
    "write must be one of the numbers you were given, copied exactly. "
    "2. If something is not in the input, say it is not available. Do not infer it. "
    "3. Do not give advice, recommendations, price targets or predictions. Describe "
    "what the figures show and what is unknown. "
    "4. Say when data is stale or missing, and prefer refusing to summarising a "
    "figure whose provenance is not stated. "
    "5. No preamble, no sign-off: three to six sentences of plain prose."
)


def _normalise(token: str) -> set[str]:
    """Every form of one numeric token that could fairly be called a match.

    Locale makes separators ambiguous — ``6.280`` is six thousand here and six
    point two eight elsewhere — so both readings are accepted instead of picking
    one and mangling the other: stripping separators outright turned ``628000.00``
    into sixty-two million. The guard is a lint, not a proof: it would rather trace
    a correctly formatted number than flag one, and everything it cannot trace it
    names.
    """
    forms = {token, token.lstrip("-")}
    digits = re.sub(r"\D", "", token)
    if digits:
        forms.add(digits)
        forms.add(digits.lstrip("0") or "0")

    # (1) comma as the thousands separator, (2) dot as the thousands separator.
    for interpretation in (token.replace(",", ""), token.replace(".", "").replace(",", ".")):
        try:
            number = Decimal(interpretation)
        except InvalidOperation:
            continue
        forms.add(str(number))
        forms.add(f"{number.normalize():f}")
        if number == number.to_integral_value():
            forms.add(str(int(number)))
    return forms


def allowed_numbers(figures: object) -> set[str]:
    """Every number the input itself contains, in every form we accept."""
    allowed: set[str] = set()
    for match in NUMBER.finditer(json.dumps(figures, default=str)):
        allowed |= _normalise(match.group())
    return allowed


def unverified_numbers(narrative: str, figures: object) -> list[str]:
    """Numbers in the narrative that cannot be traced back to the input."""
    allowed = allowed_numbers(figures)
    foreign: list[str] = []
    for match in NUMBER.finditer(narrative):
        token = match.group()
        if not (_normalise(token) & allowed):
            foreign.append(token)
    return foreign


def narratable_figures(report: ReportResult, scoreboard: Scoreboard) -> dict[str, object]:
    """The payload the model receives: figures computed here, and nothing else.

    Defined once and shared by the CLI and the UI. Two copies of this dictionary
    would drift, and the guard would end up checking the prose against a set of
    numbers that is not the set the reader was shown.

    Provenance travels with the figures on purpose: the instructions ask the model
    to say when data is stale, and it cannot do that if it is only handed values.
    """
    return {
        "generated_at": report.generated_at,
        "snapshot_as_of": report.snapshot_as_of,
        "fresh": report.fresh,
        "issues": report.issues,
        "sources": report.sources,
        "summary": report.summary,
        "realized_pnl": report.realized_pnl,
        "liquidation_costs": report.liquidation_costs,
        "scoreboard": scoreboard.model_dump(mode="json"),
    }


class AnalysisResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    narrative: str
    backend: str
    model: str
    #: False when at least one number in the narrative is not in the input.
    verified: bool
    unverified_numbers: list[str] = Field(default_factory=list)
    figures: dict[str, object] = Field(default_factory=dict)


class NarrativeService:
    """Turns computed figures into prose, and checks the prose against them."""

    def __init__(self, backend: AnalysisBackend, model: str = "local") -> None:
        self.backend = backend
        self.model = model

    async def narrate(self, figures: dict[str, object], focus: str | None = None) -> AnalysisResult:
        request = json.dumps(figures, indent=2, ensure_ascii=False, default=str)
        if focus:
            request = f"{request}\n\nConcentrate on: {focus}"

        narrative = await self.backend.complete(SYSTEM_PROMPT, request)
        foreign = unverified_numbers(narrative, figures)
        return AnalysisResult(
            narrative=narrative,
            backend=self.backend.name,
            model=self.model,
            verified=not foreign,
            unverified_numbers=foreign,
            figures=figures,
        )


def backend_from_settings(settings: Settings) -> AnalysisBackend | None:
    """The configured backend, or ``None`` when the installation has none.

    ``None`` is the shipped state and not an error: the deterministic core never
    needs a model, and the caller is expected to say so rather than fail.
    """
    if not settings.llm_base_url:
        return None
    api_key = os.environ.get(API_KEY_SECRET) or get_secret(API_KEY_SECRET)
    return OpenAICompatibleBackend(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=api_key,
        timeout_seconds=settings.llm_timeout_seconds,
    )
