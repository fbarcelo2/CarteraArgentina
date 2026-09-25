"""The MCP front-end must build against the SDK that is installed.

Nothing here is mocked, on purpose. Importing a class that moved between SDK
majors leaves the whole surface broken while every unit test still passes — the
failure only appears when a client connects. These tests exercise the real
server object: construction, the declared tool surface, the parameter schemas a
model reads, and the read-only guarantee.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pytest.importorskip("mcp", reason="the mcp extra is optional")

from cartera.mcp_server import build_server
from cartera.spec import manifest

EXPECTED_TOOLS = {
    "market_session",
    "market_quotes",
    "portfolio_import",
    "portfolio_summary",
    "portfolio_report",
    "config_show",
    "proposal_record",
    "proposal_scoreboard",
}

# Words that would betray an order-placing capability. The project's central
# promise is that no such capability exists, so it is asserted, not assumed.
ORDER_WORDS = ("order", "buy", "sell", "execute", "submit", "purchase")

SPEC_URI = "cartera://spec"
AGENT_URI = "cartera://agent"


@pytest.fixture(scope="module")
def server() -> Any:
    return build_server()


@pytest.fixture(scope="module")
def tools(server: Any) -> dict[str, Any]:
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def test_server_builds_against_the_installed_sdk(server: Any) -> None:
    assert type(server).__module__.startswith("mcp."), "the server must come from the SDK, not a stand-in"


def test_tool_surface_is_exactly_the_declared_one(tools: dict[str, Any]) -> None:
    assert set(tools) == EXPECTED_TOOLS


def test_manifest_and_server_cannot_drift(tools: dict[str, Any]) -> None:
    declared = {tool["name"] for tool in manifest()["mcp_tools"]}
    assert declared == set(tools)


def test_no_tool_is_order_capable(tools: dict[str, Any]) -> None:
    for name, tool in tools.items():
        haystack = f"{name} {tool.description or ''}".lower()
        for word in ORDER_WORDS:
            assert word not in haystack, f"{name!r} reads as order-capable ({word!r})"


def test_parameters_carry_schema_descriptions(tools: dict[str, Any]) -> None:
    """An undescribed parameter is a parameter a model guesses at."""
    for name, tool in tools.items():
        properties = (tool.input_schema or {}).get("properties") or {}
        for parameter, spec in properties.items():
            assert spec.get("description"), f"{name}.{parameter} has no schema description"


def test_spec_resource_is_readable(server: Any) -> None:
    resources = [str(resource.uri) for resource in asyncio.run(server.list_resources())]
    assert SPEC_URI in resources

    contents = asyncio.run(server.read_resource(SPEC_URI))
    payload = "\n".join(getattr(item, "content", str(item)) for item in contents)
    document = json.loads(payload)
    assert document["read_only"] is True
    assert document["executes_orders"] is False


def test_resources_and_prompts_cannot_drift(server: Any) -> None:
    """Tools are not the only surface: resources and prompts must also match."""
    resources = {str(resource.uri) for resource in asyncio.run(server.list_resources())}
    declared_resources = {item["uri"] for item in manifest()["mcp_resources"]}
    assert resources == declared_resources

    prompts = {prompt.name for prompt in asyncio.run(server.list_prompts())}
    declared_prompts = {item["name"] for item in manifest()["mcp_prompts"]}
    assert prompts == declared_prompts


def test_agent_instructions_are_served(server: Any) -> None:
    contents = asyncio.run(server.read_resource(AGENT_URI))
    text = "\n".join(getattr(item, "content", str(item)) for item in contents)

    # Executable documentation: the persona cannot silently lose its guardrails.
    assert "NEVER compute" in text
    assert "cannot trade" in text
    assert "as_of" in text, "provenance must be required"
    assert '"fresh": false' in text, "refusal handling must be stated"
    assert "not advice" in text


def test_review_prompt_carries_the_invariants(server: Any) -> None:
    result = asyncio.run(server.get_prompt("portfolio_review", {"focus": "concentration"}))
    joined = json.dumps(result, default=str)
    assert "NEVER compute" in joined
    assert "portfolio_report" in joined, "the review must start from computed figures"
    assert "concentration" in joined, "the focus argument must reach the prompt"
