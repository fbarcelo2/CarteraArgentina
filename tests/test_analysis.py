"""Narration with a guard: the model writes prose, and never numbers of its own.

The adapter is exercised against a real HTTP server on loopback rather than a
mocked client, because the failure this guards against is a shape mismatch between
what the SDK sends and what the endpoint answers — and a mock would agree with
whatever the code does.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from cartera.adapters.openai_compat import AnalysisBackendError, OpenAICompatibleBackend
from cartera.app.analysis import NarrativeService, backend_from_settings, unverified_numbers
from cartera.config import load_settings

FIGURES: dict[str, object] = {
    "generated_at": "2026-09-25T16:00:00-03:00",
    "fresh": True,
    "summary": {"valuations": {"ARS": {"market_value": "628000.00"}}},
    "scoreboard": {"hit_rate_pct": "50.00", "total_proposals": 4},
}

REPLY: dict[str, str] = {}
RECORDED: list[dict[str, object]] = []


class _Handler(BaseHTTPRequestHandler):
    """Answers whatever REPLY says, and records what it was asked."""

    def do_POST(self) -> None:  # the name http.server calls for
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) or b"{}"
        try:
            RECORDED.append(json.loads(raw))
        except ValueError:
            RECORDED.append({"unparsable": raw.decode(errors="replace")})
        self.send_response(int(REPLY.get("status", "200")))
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(REPLY.get("body", "{}").encode())

    def log_message(self, *args: object) -> None:
        """Silence the default stderr logging: it would drown the test output."""


@pytest.fixture
def endpoint() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    RECORDED.clear()
    REPLY.clear()
    yield
    RECORDED.clear()
    REPLY.clear()


class FakeBackend:
    """Stands in for the model, so the guard can be tested without one."""

    name = "fake"

    def __init__(self, answer: str = "", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if self.error is not None:
            raise self.error
        return self.answer

    async def aclose(self) -> None:
        self.closed = True


def build_settings(tmp_path: Path, **overrides: str):
    return load_settings(env_file=tmp_path / "nonexistent.env", overrides=dict(overrides))


# -- the guard ---------------------------------------------------------------


def test_a_number_that_is_not_in_the_input_is_reported() -> None:
    assert unverified_numbers("The portfolio is worth 999999 pesos.", FIGURES) == ["999999"]


def test_repeating_a_figure_is_not_flagged() -> None:
    narrative = "The portfolio is worth 628000.00 pesos across 4 proposals."
    assert unverified_numbers(narrative, FIGURES) == []


def test_thousands_separators_in_the_local_style_are_traced() -> None:
    """6.280 here is 6280 elsewhere: the guard compares digits, not typography."""
    assert unverified_numbers("Valuation reached 628.000 pesos.", FIGURES) == []


def test_a_trailing_percent_sign_does_not_hide_a_match() -> None:
    assert unverified_numbers("The hit rate is 50%.", FIGURES) == []


def test_a_number_hidden_inside_prose_is_still_found() -> None:
    assert unverified_numbers("It grew 12.5 percent, roughly.", FIGURES) == ["12.5"]


async def test_a_clean_narrative_is_verified() -> None:
    backend = FakeBackend("The portfolio is worth 628000.00 pesos across 4 proposals.")
    result = await NarrativeService(backend).narrate(FIGURES)

    assert result.verified is True
    assert result.unverified_numbers == []
    assert result.backend == "fake"


async def test_a_narrative_with_one_invented_number_is_not_verified() -> None:
    backend = FakeBackend("The portfolio is worth 628000.00 pesos, a gain of 98765 pesos.")
    result = await NarrativeService(backend).narrate(FIGURES)

    assert result.verified is False
    assert result.unverified_numbers == ["98765"]
    assert result.figures == FIGURES, "the input is returned so the reader can check the claim"


async def test_the_request_carries_the_invariants_and_the_figures() -> None:
    backend = FakeBackend("nothing to report")
    await NarrativeService(backend).narrate(FIGURES, focus="concentration")

    system_prompt, user_prompt = backend.calls[0]
    assert "NEVER compute" in system_prompt
    assert "628000.00" in user_prompt
    assert "concentration" in user_prompt


async def test_a_backend_failure_is_raised_not_swallowed() -> None:
    backend = FakeBackend(error=AnalysisBackendError("the model is down"))
    with pytest.raises(AnalysisBackendError):
        await NarrativeService(backend).narrate(FIGURES)


# -- configuration -----------------------------------------------------------


def test_no_backend_configured_is_not_an_error(tmp_path: Path) -> None:
    """The shipped state: the core computes everything without a model."""
    assert backend_from_settings(build_settings(tmp_path)) is None


def test_a_configured_backend_is_built_from_settings(tmp_path: Path) -> None:
    settings = build_settings(
        tmp_path,
        CARTERA_LLM_BASE_URL="http://127.0.0.1:8080/v1",
        CARTERA_LLM_MODEL="qwen3-4b",
    )
    backend = backend_from_settings(settings)

    assert isinstance(backend, OpenAICompatibleBackend)
    assert backend.endpoint == "http://127.0.0.1:8080/v1/chat/completions"
    assert backend.model == "qwen3-4b"
    assert backend.temperature == 0.0, "narration must be reproducible by default"


# -- the adapter, over real HTTP ---------------------------------------------


async def test_the_adapter_returns_the_content_and_sends_the_openai_shape(endpoint: str) -> None:
    REPLY["body"] = json.dumps({"choices": [{"message": {"content": "  a short note  "}}]})
    backend = OpenAICompatibleBackend(base_url=endpoint, model="qwen3-4b", api_key="secret-key")
    try:
        assert await backend.complete("system", "user") == "a short note"
    finally:
        await backend.aclose()

    sent = RECORDED[0]
    assert sent["model"] == "qwen3-4b"
    assert sent["stream"] is False
    assert sent["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]


async def test_the_adapter_reports_an_http_error_with_its_status(endpoint: str) -> None:
    REPLY["status"] = "500"
    REPLY["body"] = json.dumps({"error": "the model crashed"})
    backend = OpenAICompatibleBackend(base_url=endpoint, model="m")
    try:
        with pytest.raises(AnalysisBackendError, match="500"):
            await backend.complete("system", "user")
    finally:
        await backend.aclose()


async def test_the_adapter_reports_a_body_that_is_not_json(endpoint: str) -> None:
    REPLY["body"] = "<html>a proxy said no</html>"
    backend = OpenAICompatibleBackend(base_url=endpoint, model="m")
    try:
        with pytest.raises(AnalysisBackendError, match="not JSON"):
            await backend.complete("system", "user")
    finally:
        await backend.aclose()


async def test_the_adapter_reports_an_empty_narration(endpoint: str) -> None:
    REPLY["body"] = json.dumps({"choices": [{"message": {"content": "   "}}]})
    backend = OpenAICompatibleBackend(base_url=endpoint, model="m")
    try:
        with pytest.raises(AnalysisBackendError, match="empty"):
            await backend.complete("system", "user")
    finally:
        await backend.aclose()


async def test_an_unreachable_backend_names_the_endpoint_and_not_the_key() -> None:
    backend = OpenAICompatibleBackend(base_url="http://127.0.0.1:9/v1", model="m", api_key="super-secret")
    try:
        with pytest.raises(AnalysisBackendError) as failure:
            await backend.complete("system", "user")
    finally:
        await backend.aclose()

    message = str(failure.value)
    assert "127.0.0.1:9" in message
    assert "super-secret" not in message
