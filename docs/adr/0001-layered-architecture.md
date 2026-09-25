# ADR 0001 — Layered architecture with a pure domain

- Status: accepted
- Date: 2026-09-25

## Context

The project that inspired this one put its business logic inside HTTP route
handlers (portfolio arithmetic lived in the router file). It also hard-wired
one vendor SDK into the analysis path and one broker's site into the data path.
The predictable results: the arithmetic cannot be tested without a web server,
the vendor cannot be swapped without editing business code, and nothing prevents
front-ends from diverging.

## Decision

Four layers with one direction of dependency:

`domain` (pure) ← `app` (use cases) ← front-ends (`cli`, `mcp_server`), with
`ports` defining what `adapters` must satisfy.

Rules:

1. `domain` imports nothing but the standard library and `pydantic`. No network,
   no database, no `datetime.now()`.
2. Every external interaction goes through a `port`.
3. Business rules appear in exactly one place: `app/services.py` or `domain`.
4. Front-ends translate input and output, and nothing else.

## Consequences

- Domain and metrics are testable with plain values: no fixtures for servers, no
  clock freezing. 57 tests run in under a second.
- Adding a data source means writing an adapter, not editing analysis logic.
- Adding a web UI later is a new directory, not a rewrite.
- Cost: more files and one more indirection than a single-module script. The
  first feature took longer; the fourth will not.

## What this is not

This is not "clean architecture" as ceremony. It is the minimum structure needed
so three front-ends cannot drift, and so arithmetic can be tested in
milliseconds. Anything beyond that would be unearned.
