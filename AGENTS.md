# AGENTS.md — CarteraArgentina

Instructions for AI agents (and humans) contributing to this repository.

## Invariants — do not violate these

1. **Never add order execution.** No adapter, tool, CLI command or HTTP endpoint
   may place, modify or cancel an order. This is the project's core promise and
   it is structural: a capability that does not exist cannot be reached by a
   prompt injection.
2. **Never store credentials for a login-protected third party.** Secrets belong
   in the OS keyring. Nothing that authenticates with a user identity against a
   scraped website belongs in this repository.
3. **Never let a model produce a number that reaches the user as fact.** Metrics
   are computed in `domain/` with tests. A model may summarise, compare or
   explain; if it computes, that is a bug.
4. **Never publish market data.** `data/` is gitignored. Test fixtures must be
   synthetic; do not commit quotes or research PDFs from a vendor.
5. **Never write a report on data that failed a gate.** Refusing with reasons is
   the correct behaviour, not an error path to be smoothed over.
6. **Never rewrite history.** The ledger and audit log are append-only, enforced
   by database triggers. Corrections are appended, never applied in place.

## Conventions

- **Language: English** for code, identifiers, comments, CLI copy, docs and
  commit messages. This repository is public; mixed languages are debt from day one.
- **Layering:** `domain` (pure, no I/O) ← `app` (use cases) ← front-ends
  (`cli`, `mcp_server`). Adapters implement ports; nothing else touches the world.
- **Money:** `Decimal` only. Convert external floats via `cartera.domain.money.as_decimal`.
- **Commits:** Conventional Commits (`feat`, `fix`, `docs`, `refactor`, `test`,
  `chore`, `perf`, `ci`). No AI attribution trailers.
- **Tests:** every behaviour change needs a test. Pure logic goes in `tests/`
  without a network; use `tests/fakes.py` for a market source with controlled
  timestamps, and `respx` for HTTP contract tests.
- **Errors:** specific types from `cartera.domain.errors`. Distinguish
  "unavailable" from "stale"; they require different responses.

## Commands

```bash
uv sync                     # install (dependencies are pinned by uv.lock)
uv run ruff check .         # lint, must be clean
uv run pytest -q            # tests, must be green
uv run cartera doctor       # end-to-end health check (hits the live source)
uv run cartera spec --json  # capability manifest
```

## When changing the market-data contract

The adapter targets a public portal whose response shapes were observed live and
are pinned by `tests/test_byma_open_contract.py`. If the portal changes, update
the fixtures from a real response and adjust the parser — do not weaken the
contract test to make it pass.
