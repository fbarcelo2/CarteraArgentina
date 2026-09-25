# Portfolio snapshot format

A snapshot is a JSON file you produce yourself (or export, once a broker adapter
exists). It describes what you hold *at a point in time* and how much cash is in
each currency. The tool never sees your broker credentials to read it.

## Schema

```json
{
  "as_of": "2026-02-10T11:00:00-03:00",
  "source": "manual-export",
  "label": "septiembre",
  "cash": { "ARS": "300000", "USD": "50" },
  "lots": [
    {
      "lot_id": "ggal-2026-02-10-1",
      "ticker": "GGAL",
      "asset_type": "equity",
      "quantity": "100",
      "unit_price": "5000",
      "fees": "1650",
      "currency": "ARS",
      "settlement": "1",
      "opened_at": "2026-02-10T11:00:00-03:00"
    }
  ]
}
```

## Field reference

| Field | Required | Notes |
| --- | --- | --- |
| `as_of` | yes | ISO-8601 with offset. Used to date the snapshot, never to value it. |
| `cash` | no | Per-currency amounts. Omitted currencies are zero. |
| `lots[]` | no | Empty is valid (a cash-only portfolio). |
| `lot_id` | yes | Must be stable and unique: re-importing the same id is a no-op, a *new* id for the same purchase is a new lot. |
| `ticker` | yes | Uppercased automatically. |
| `asset_type` | yes | `equity`, `cedear`, `bond`, `corp_bond`, `leter`, `option`, `fund`. Drives the fee schedule. |
| `quantity` | yes | Greater than zero. Selling is recorded as a transaction, not as a negative lot. |
| `unit_price` | yes | Price per unit at purchase, commission excluded. |
| `fees` | no | Total commission paid for the purchase. Part of the cost basis. |
| `currency` | yes | `ARS` or `USD`. |
| `settlement` | no | `1` (CI), `2` (24hs), `3` (48hs). Defaults to CI. |
| `opened_at` | yes | ISO-8601 with offset. |

Amounts may be strings or numbers; strings are recommended because JSON floats
cannot represent every decimal exactly.

## Why lots instead of positions

A position ("100 GGAL") cannot answer "which of my purchases should I sell
first?" A lot can. Keeping purchases separate is what makes tax-aware selling and
true per-lot P&L possible, so the format forces you to describe purchases, not
aggregates. Aggregation happens at read time, in `PortfolioSnapshot.positions()`.

## Immutability

Importing appends. There is no "edit" or "delete": the database rejects both. If
you made a mistake, append the correction as a new snapshot and record it as an
adjustment transaction — the same discipline as an accounting ledger, for the
same reason.
