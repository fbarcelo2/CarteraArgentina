# Data sources, and what they are safe for

Every claim here was verified with a live call on 2026-09-25, not read from
documentation. Numbers are quoted so they can be re-checked.

The reason this file exists: a price series is only useful if you know whether a
corporate action has been applied to it. A raw series renders YPF's 10:1 split as
a one-day drop of -90.2%, which is not a market event but a bookkeeping one, and
every moving average, RSI and range computed from it would be wrong in a way that
looks like a signal.

## Daily historical series

### data912.com — the base series

```
GET https://data912.com/historical/{stocks|cedears|bonds}/{TICKER}
```

No key, no headers, documented at 120 requests/min. Daily OHLCV plus `dr` (daily
return) and `sa` (documented as annualised sigma).

Depth: GGAL 6291 bars from 2001-01-02, AAPL CEDEAR 3369 from 2012-11-13, AL30 1227
from 2021-09-16. Around five years is the ceiling for Argentine sovereign bonds
here, and no other credential-free source returned Argentine bonds at all.

**It is raw, and that is the point of the warning above.** Three corporate actions
prove it: BYMA's 2:1 split on 2025-05-26 arrives as `dr` = -0.4768, YPF's 10:1
(effective 2026-08-04) as `dr` = -0.9023, and the SPY CEDEAR ratio change from
20:1 to 60:1 on 2026-05-29 as `dr` = -0.665. The `dr` and `sa` fields carry the
artificial jump too, so they are not a way around it. Some CEDEAR-side ratio
events are smoothed while others are not — the behaviour is inconsistent, so it
cannot be relied on in either direction.

### Yahoo Finance v8 chart — the adjustment authority

```
GET https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}
    ?period1=<epoch>&period2=<epoch>&interval=1d&includeAdjustedClose=true&events=div,split
```

Returns `close` adjusted for splits and `adjclose` additionally adjusted for
dividends, plus an `events` object with the splits and dividends dated. That is
where an adjustment factor table comes from. Verified against real actions:
YPFD.BA is continuous across its 10:1, BYMA.BA across its 2:1, AAPL across the
4:1; GGAL.BA reads 6290.0 on 2026-09-25 against an adjclose of 4963.946.
GGAL.BA goes back to 2000-07-26 (6514 bars).

Caveats, all of them real: it carries **no Argentine sovereign bonds**
(AL30.BA, GD30.BA and GD35.BA return "No data found"); from this host a plain
curl gets HTTP 429 while a real browser gets 200, so it needs a browser-capable
fetch; `range=max` collapses to about 169 coarse points, so use `period1`/`period2`;
and the terms are personal, non-commercial.

### BYMA Open Data — the reconciliation series

There is a historical endpoint in the same free namespace the project already uses,
reachable only by GET and only with `Origin` and `Referer` set to
`open.bymadata.com.ar`:

```
GET https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free/chart/historical-series/history
    ?symbol=GGAL%2024HS&resolution=D&from=<epoch>&to=<epoch>
```

The settlement suffix (` 24HS`) is mandatory, and it covers equities, CEDEARs and
bonds. It returns a rolling window of about two years (486 bars ending today; the
window is server-side, `from=2001` returns the same window).

It is back-adjusted, but with a **confirmed hole**: BYMA's own 2:1 split of
2025-05-26 is not adjusted (the chart halves from 375.904 to 195.103). So it is a
cross-check, not the price of record.

### Dead ends, checked and closed

| Source | Verdict |
| --- | --- |
| Stooq free CSV | JS bot wall, and no Argentine instruments in its database |
| IOL API | HTTP 401, requires a login — out by rule |
| Nasdaq Data API | US listings only; its "GGAL" is the ADR, a different instrument |
| Rava | no public API; would mean reverse-engineering a publisher's app |
| Bolsar, Ámbito historical pages | blocked or empty |

### What this implies for the adapter

Base series from data912 (one source, three instrument classes, deepest history),
a corporate-action factor table derived from Yahoo's `events`, applied to that
series, and a daily reconciliation against BYMA's chart series. No single source
from this list ships as the series of record without an adjustment layer.

## Fundamentals

### SEC EDGAR XBRL — official, keyless, for the CEDEAR underlyings

```
GET https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/{Tag}.json   # one line item, ~2 KB
GET https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json                   # full fact set, 3-5 MB
GET https://www.sec.gov/files/company_tickers.json                                  # ticker -> CIK map
```

Every fact carries form, period and accession number, so the provenance this
project requires comes free with the data. Two hard operational rules: a
descriptive `User-Agent` is mandatory (an empty one returns HTTP 403), and stay
under the published 10 requests/second. The multi-megabyte payloads belong in a
local cache refreshed weekly, not in a request path.

Resolve tickers through `company_tickers.json` and **never guess a CIK**: the one
assumed for the GGAL ADR (0001114707) belongs to a private individual, and a
guessed CIK returns another filer's documents with a cheerful HTTP 200.

### Sector and industry classification

For US issuers, and therefore for the companies behind CEDEARs, the submissions
endpoint carries it:

```
GET https://data.sec.gov/submissions/CIK##########.json
  -> sic: 3571, sicDescription: "Electronic Computers", fiscalYearEnd, stateOfIncorporation
```

Verified for Apple on 2026-09-25. For Argentine local issuers there is no free
classification source: BYMA does not classify and the CNV publishes no dataset.

### The gap that money does not close

Financial statements of Argentine issuers. The CNV publishes documents rather than
an API or a dataset (`datos.gob.ar` package search for "cnv" returns zero results)
and BYMA's richer endpoints return 401. Local issuers are reachable only by parsing
CNV PDFs or through the same company's US listing. The CEDEAR-to-underlying mapping
itself has not been verified against an authoritative source yet.

## News

Recorded, not implemented: the decision is that news arrives as text pasted by the
user, so no adapter is written and no feed is polled. Should that change, these were
verified live and carry title, link and timestamp with no key: Infobae Economía ARC
(100 items, minute-level timestamps, the freshest), Ámbito Economía (20), La Nación
Economía ARC (85 — the articles are paywalled, consume the feed only), Perfil (70)
and Buenos Aires Times (100, English). El Cronista has no working feed, the CNV
publishes no feed, and BCRA comunicados are HTML with no machine-readable dates.

For FX context, keyless: DolarAPI `/v1/dolares` (six or more rates, each with its
own update timestamp) and the BCRA's `estadisticascambiarias/v1.0/Cotizaciones`
(the official daily reference; the v3.0 path returns 410, deprecated).
