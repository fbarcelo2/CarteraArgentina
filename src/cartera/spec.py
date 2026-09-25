"""Machine-readable manifest of what this installation can do.

Rationale (project standard, auto-discovery): an agent or an operator must be
able to ask a running installation what it exposes instead of reading docs that
drift. Both front-ends serve this same manifest — the CLI as ``cartera spec``
and the MCP server as a resource.
"""

from __future__ import annotations

from typing import Any

from cartera import __version__

CLI_COMMANDS: tuple[str, ...] = (
    "config show",
    "config hydrate",
    "doctor",
    "market quotes",
    "market session",
    "portfolio import",
    "portfolio summary",
    "report",
    "spec",
)

MCP_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "market_session",
        "description": "Whether the market trades today, plus opening and closing times.",
    },
    {
        "name": "market_quotes",
        "description": "Delayed quotes for specific tickers. Read-only; no credentials.",
        "arguments": {"tickers": ["GGAL"], "universes": ["cedears"]},
    },
    {
        "name": "portfolio_import",
        "description": "Import a local JSON snapshot into the append-only ledger.",
        "arguments": {"path": "/home/user/mi-cartera.json", "label": "septiembre"},
    },
    {
        "name": "portfolio_summary",
        "description": "Holdings and cash from the latest imported snapshot.",
    },
    {
        "name": "portfolio_report",
        "description": (
            "Deterministic valuation, unrealized/realized P&L and gate results. "
            "Refuses to produce numbers when quotes are stale."
        ),
    },
    {"name": "config_show", "description": "Effective non-secret configuration."},
)

MCP_RESOURCES: tuple[dict[str, Any], ...] = (
    {"uri": "cartera://spec", "description": "This manifest: what the installation exposes."},
    {"uri": "cartera://agent", "description": "Portable analyst instructions, for any MCP client."},
)

MCP_PROMPTS: tuple[dict[str, Any], ...] = (
    {
        "name": "portfolio_review",
        "description": "Instructions to review the latest snapshot, optionally focused on one area.",
        "arguments": {"focus": "concentration"},
    },
)

GUARANTEES: tuple[str, ...] = (
    "no order execution capability",
    "no broker credentials stored",
    "metrics computed in code, never by a model",
    "append-only ledger enforced by database triggers",
    "hash-chained audit log",
    "stale quotes refused instead of reported",
)


def manifest() -> dict[str, Any]:
    """Return the capability manifest for this installation."""
    return {
        "name": "cartera-argentina",
        "version": __version__,
        "kind": "portfolio-analytics",
        "read_only": True,
        "executes_orders": False,
        "cli": list(CLI_COMMANDS),
        "mcp_tools": list(MCP_TOOLS),
        "mcp_resources": list(MCP_RESOURCES),
        "mcp_prompts": list(MCP_PROMPTS),
        "guarantees": list(GUARANTEES),
        "docs": {"architecture": "docs/ARCHITECTURE.md", "adr": "docs/adr/"},
    }
