# Using CarteraArgentina with an AI agent

The analyst persona lives **in this package**, not in any one harness's
configuration: the server serves it as an MCP resource and as a prompt, so any
MCP client picks up the same invariants without anyone copy-pasting a system
prompt. Nothing here is Hermes-specific.

## What the server exposes

| Surface | Name | Purpose |
| --- | --- | --- |
| Tools | `market_session`, `market_quotes`, `portfolio_import`, `portfolio_summary`, `portfolio_report`, `config_show` | the capabilities a model can call |
| Resource | `cartera://spec` | machine-readable manifest of the installation |
| Resource | `cartera://agent` | the analyst instructions (the persona) |
| Prompt | `portfolio_review` | a review request, argument `focus` optional |

Read the persona yourself:

```bash
uv run cartera-mcp   # over stdio; any client, or `mcp` inspector, will show it
```

## The invariants the persona enforces

These are stated in the instructions **and** asserted by
`tests/test_mcp_server.py`, so they cannot be lost in a refactor:

1. **Never compute a figure.** Every price, P&L, weight and total comes from a
   tool. A missing figure is reported as missing, never derived or recalled.
2. **Always carry provenance.** Source and observation time travel with every
   quote; a delayed feed is stated as delayed.
3. **A refusal is relayed, not worked around.** When a tool returns
   `{"ok": false}` or a report returns `"fresh": false`, the reason is the answer.
4. **No trading.** There is no order tool; if asked to trade, the agent says it
   cannot be done here.
5. **Scenarios are labelled as scenarios**, never as forecasts.
6. **Analysis, not advice.**

## Connecting a client

### Generic stdio client (works for most hosts)

```json
{
  "mcpServers": {
    "cartera": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/CarteraArgentina", "cartera-mcp"],
      "env": { "CARTERA_DATA_DIR": "/home/you/.local/share/cartera" }
    }
  }
}
```

Verified alternative, no `uv` wrapper needed once the project is installed:

```json
{ "command": "/absolute/path/to/CarteraArgentina/.venv/bin/cartera-mcp" }
```

Both were exercised from an unrelated working directory: passing `--directory`
matters, because a bare `uv run` resolves against the client's cwd.

### Hermes Agent

```bash
hermes mcp add cartera \
  --command uv \
  --args run --directory /absolute/path/to/CarteraArgentina cartera-mcp
hermes mcp list
hermes mcp test cartera
```

`--args` must come last. Add `--env CARTERA_DATA_DIR=/home/you/.local/share/cartera`
to point at a specific ledger.

### Claude Code

```bash
claude mcp add cartera -- uv run --directory /absolute/path/to/CarteraArgentina cartera-mcp
```

## Local model or cloud model?

The persona is model-agnostic; the client decides. The real tradeoff is not
capability, it is where your portfolio goes:

- **Local** (e.g. a local OpenAI-compatible server): holdings never leave the
  machine. Narration of already-computed figures is a small-model task, so this
  is viable — but expect weaker prose and weaker cross-checking.
- **Cloud**: better synthesis and comparison, at the cost of sending your
  positions, their sizes and your P&L to the provider.

What never depends on the model either way: the arithmetic. It is computed in
this package, and the tools return the same numbers to any client.

## What the agent must not be trusted with

The design assumes the model will eventually be wrong, or manipulated by text it
reads. Nothing in the MCP surface can act on a portfolio: no order path exists,
so no prompt injection can reach one. Keep it that way — see `AGENTS.md`.
