# Architecture

## The one-sentence version

Three thin front-ends (CLI, MCP, planned web UI) over one engine, where the
engine's arithmetic is pure and tested, and the only thing a language model ever
does is narrate numbers it did not compute.

## Layers

```
          cli.py          mcp_server.py        (web UI, planned)
             \                 |                    /
              \                |                   /
               +--------- app/services.py --------+
                          (use cases)
                              |
              +---------------+---------------+
              |               |               |
           domain/         ports/         adapters/
        (pure rules)    (Protocols)   (market data, FX, SQLite)
```

### `domain/` — no I/O, no clock, no network

- `money.py`: `Decimal` arithmetic. A float never touches a monetary value;
  floats arriving from JSON go through their shortest round-trip repr so `0.33`
  stays `0.33`.
- `models.py`: lots, transactions, positions, quotes. A `Lot` is a single
  purchase and is never merged away, which is what makes lot-specific selling
  and per-lot tax reasoning possible.
- `metrics.py`: valuation, unrealized and realized P&L, commission, weights,
  liquidation cost. Same inputs, same outputs.
- `rules.py`: gates. Freshness and concentration. A stale quote is an **error**,
  never a warning.

Because the domain has no I/O, its tests need no database, no network and no
freezing of time.

### `ports/` — the contracts

`MarketDataSource`, `PortfolioStore`, `AnalysisBackend`. Adding a data source or
a storage backend must never require touching business rules; the error taxonomy
is part of the contract, which is why "source unavailable" and "quote is stale"
are distinct exception types with distinct responses.

### `adapters/` — the only place that touches the world

- `byma_open.py`: market data from the exchange's public open-data portal. The
  contract was verified live and pinned by contract tests, including its two
  response shapes (paginated envelope vs bare array) and its POST-with-JSON-body
  quirk.
- `fx_dolarapi.py`: reference FX rates, kept in their own type so a rate can
  never be mistaken for a position.
- `sqlite_store.py`: append-only ledger with a hash-chained audit log.

### `app/services.py` — use cases

Business flows live here exactly once. This is the layer both front-ends call;
duplicating a rule in a front-end is the failure mode this structure exists to
prevent.

## Decisions that carry the safety properties

1. **No order execution anywhere.** There is no code path, no tool and no
   adapter that can place an order. The guarantee is structural, not a prompt
   instruction: a prompt injection cannot reach a capability that does not exist.
2. **The model narrates, the code computes.** Metrics are produced by tested
   Python. A model may summarise, explain or compare; it never supplies a number
   that reaches the user as fact.
3. **Refusal is a feature.** When quotes are stale, or the source is down, or no
   snapshot exists, the report is withheld *with reasons*. An authoritative
   looking number computed on yesterday's prices is the worst possible output.
4. **Append-only history, enforced by the database.** Triggers abort
   `UPDATE`/`DELETE` on the ledger; the audit log is a hash chain. Without
   immutable history, a past proposal cannot be scored, and a system that cannot
   be scored cannot improve.
5. **Provenance on every price.** Each quote carries source and timestamp, and
   the freshness gate consumes them.

## Deliberate non-goals

- Multi-tenancy, JWT/OAuth, RLS: this is a single-user local tool. Those
  controls protect against a threat model (hostile co-tenants, exposed
  multi-tenant API) that does not exist here; adding them would be cargo cult.
- Redis or any external cache: SQLite plus HTTP timeouts is enough, and an
  external service raises the barrier to entry for anyone cloning the repo.
- A hosted service: this is meant to be run by its owner, on their machine.
