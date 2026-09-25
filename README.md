# CarteraArgentina

Read-only portfolio analytics for Argentine capital markets.

Ingest market data from a public, credential-free source, keep your own portfolio
in an append-only ledger, and get **deterministic** metrics plus an AI-written
narrative — with a hard guarantee that no number a model produces ever reaches
you unchecked, and that nothing in this project can place an order.

**This is not financial advice, and it is not a trading bot.** There is no
order-execution code path anywhere in the package, by design: nothing an LLM
writes, and nothing a prompt injection could inject, can move money.

## Why it exists

Most "AI trading" projects confuse two things: computing numbers and narrating
them. The first must be exact, auditable and reproducible; only the second is a
language-model job. CarteraArgentina splits them:

- **Metrics are computed in Python**, with `Decimal` arithmetic and tests. Same
  inputs, same outputs, every time.
- **The model only narrates** numbers it receives, and never sees credentials.
- **Every price carries provenance** (source + timestamp) and is refused by the
  freshness gate when it is too old. A report on stale data is worse than no
  report, because it looks authoritative.

## Status

Phase 0 — read-only core. No broker credentials are used or stored.

| Capability | State |
| --- | --- |
| Market data ingest (public delayed feed) | working, contract-tested |
| Reference FX rates | working |
| Portfolio import from a local snapshot | working |
| Lot-based metrics, realized/unrealized P&L | working, unit-tested |
| Append-only ledger with hash-chained audit log | working, tested |
| Freshness / concentration gates | working |
| CLI | working |
| MCP server (stdio) | working, verified with a real client |
| Analyst persona (MCP resource + prompt) | working, invariant-tested |
| AI narrative | interface only, no backend bundled |
| Web UI | planned |
| Broker adapters (order placement) | **not planned for this repository** |

## Install

```bash
git clone https://github.com/<your-user>/cartera-argentina.git
cd cartera-argentina
uv sync
uv run cartera doctor
```

## Quick start

```bash
# 1. Check environment, database and data-source health
uv run cartera doctor

# 2. Fetch quotes (no credentials involved)
uv run cartera market quotes GGAL AL30 AAPL --json

# 3. Import your portfolio snapshot (see docs/portfolio-snapshot.md)
uv run cartera portfolio import ~/mi-cartera.json

# 4. Build the deterministic report
uv run cartera report
```

MCP clients (Claude Code, Hermes, any MCP host). Pass `--directory`: a bare
`uv run` would otherwise resolve against the client's working directory.

```json
{
  "mcpServers": {
    "cartera": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/CarteraArgentina", "cartera-mcp"]
    }
  }
}
```

Tools exposed: `market_session`, `market_quotes`, `portfolio_import`,
`portfolio_summary`, `portfolio_report`, `config_show`. The server also serves the
analyst instructions as the resource `cartera://agent` and as the
`portfolio_review` prompt, so a client inherits the invariants without a
copy-pasted system prompt. See `docs/agent.md`. The machine-readable manifest is
`uv run cartera spec --json`.

## Data sources

| Source | Credentials | Cost | Notes |
| --- | --- | --- | --- |
| Public market portal (delayed) | none | free | default; undocumented public endpoint, 20-minute delay |
| Reference FX API | none | free | official/MEP/blue reference rates |
| Official exchange market-data API | required | paid plans | supported via the port; bring your own credentials |

A source that stops answering is reported as *unavailable*, never as data: the
distinction matters, and it is why the port has explicit error types.

## Architecture

```
src/cartera/
├── domain/     pure rules: money (Decimal), models, metrics, gates. No I/O.
├── ports/      Protocols the outside world must satisfy
├── adapters/   market data, FX, SQLite (append-only ledger)
├── app/        use cases composing domain + ports
├── agent.py    analyst instructions, served over MCP (harness-agnostic)
├── cli.py      thin front-end (typer)
└── mcp_server.py  thin front-end (MCP over stdio)
```

Three front-ends, one engine. See `docs/ARCHITECTURE.md` for the decisions
behind the layering and `docs/adr/` for the log.

## Development

```bash
uv sync                        # pinned dependencies, plus the validation tools
scripts/install-validators.sh  # one-time: shellcheck, actionlint, taplo, typos, gitleaks, hadolint
scripts/validate.sh            # everything: lint, types, security, deps, secrets, tests, coverage
scripts/validate.sh --quick    # static only
```

| Check | Tool |
| --- | --- |
| Lint and format | ruff |
| Static types | pyright (venv resolved via `pyrightconfig.json`) |
| Python security | bandit, semgrep |
| Dependency hygiene | deptry, pip-audit |
| Dead code | vulture |
| Workflows, YAML, TOML, shell, Dockerfile | actionlint, yamllint, taplo, shellcheck, hadolint |
| Spelling, secrets in history | typos, gitleaks |
| Dynamic: tests and coverage | pytest, coverage (gate at 70%) |

CI runs `scripts/validate.sh` itself, so the pipeline and the local loop check the
same things. A missing tool is reported as SKIP, never as a silent pass.

## Guarantees

- No order placement, no broker credentials in this repository.
- The ledger is append-only **enforced by the database** (triggers abort
  `UPDATE`/`DELETE`), so the history used to score past proposals cannot be
  rewritten, not even by this code.
- Audit entries are hash-chained: removing or editing one breaks verification.
- Money never passes through a binary float.
- Secrets live in the OS keyring, never in a committed file.

## License

Apache-2.0 — see `LICENSE`.
