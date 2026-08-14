# NQGEX

**An options-derived gamma exposure model for NQ / MNQ futures — data pipeline, analytics engine, dashboard, and live level delivery to two trading platforms.**

> Personal project, shown here as a portfolio piece.

![NQGEX dashboard](screenshots/dashboard-overview.png)

## Overview

NQGEX ingests a full options chain on a schedule, computes a dealer gamma exposure model across roughly 12,000 contracts, and publishes the resulting price levels to a browser dashboard and directly onto live trading charts.

Each run produces:

| Output | What it represents |
|---|---|
| **Net GEX & regime** | Aggregate dealer gamma per 1% move, and the hedging regime its sign implies |
| **Gamma flip** | The price at which the map's net gamma changes sign, located by re-pricing the entire book across a spot grid |
| **Call wall / put wall** | The strikes carrying the heaviest gamma on each side — where opposing hedge flow concentrates |
| **Secondary walls** | Notable opposite-side concentrations across the zero-gamma level, published only when they clear a set of significance, composition and separation tests |
| **γ-ladder** | The remaining strikes that stand out as genuine local concentrations rather than shoulders of larger ones, each tagged with its dominant side |
| **Expected move** | The ~1σ range into the front expiry, from at-the-money implied volatility |
| **ATM IV / IV30** | Front-expiry volatility and a 30-day interpolated figure for term-structure context |
| **Positioning** | Open-interest and gamma ratios across the near band |

Levels are computed in options space and translated into futures points through a multiplier derived live on every run rather than hardcoded — it bundles every conversion factor between the options underlying and the futures contract, all of which drift intraday.

## The dashboard

A single self-contained dark-theme page, regenerated on every snapshot and self-refreshing in the browser. No server, no build step, no external dependencies — the charts are hand-built SVG.

![Charts and session drift](screenshots/charts-and-session-drift.png)

- **Session Read** — an auto-generated plain-English synthesis of the current map: regime, spot against the flip, walls with distances, the priced range, vol term structure and positioning
- **γ by Strike** — the per-strike gamma histogram, calls against puts, with every published level overlaid
- **Net GEX vs Price** — the re-priced gamma curve whose zero crossing defines the flip
- **Session Drift** — how the levels, the expected-move band and net gamma have migrated through the session, with a per-snapshot regime strip
- **Table twins** — every chart has a tabular equivalent for exact figures
- **Inline explainers** — every metric, chart and status chip carries a plain-English note on what it is and how it was computed
- **A visible data ledger** — chips showing the status and vintage of every input, and how old the underlying data is. Fetch failures surface on the page rather than degrading silently

## Chart integrations

Both platforms receive the same levels with the same styling. They differ only in how the data reaches them, which is dictated entirely by what each platform's scripting environment permits.

| | NinjaTrader 8 | TradingView |
|---|---|---|
| Language | C# (NinjaScript) | Pine Script |
| Delivery | **Automatic** — reads from disk | Paste string |
| Refresh | Every 30 s, unattended | On paste |
| Constraint | Full filesystem access | Sandboxed: no network, no disk |

### NinjaTrader 8

A custom indicator reads the collector's level file directly off disk, so the chart keeps itself current with no interaction:

```
options chain  →  collector  →  level file  →  indicator  →  chart
                   scheduled                    every 30 s
```

A timer compares the file's modified time against what it last parsed and exits immediately when unchanged — a stat call against a few hundred bytes, a rounding error against one CPU core.

Worth stating plainly: **the levels do not come from NinjaTrader's datafeed.** The platform supplies price bars and the price scale; every level originates from options data processed outside it. The indicator keeps drawing with the datafeed disconnected.

Zones, lines and labels are painted in a single owned render pass, so each line terminates where its label begins instead of striking through it. A status line reports the age of the level file and turns amber when the feed stalls — the platform has no status chips of its own, and a stalled feed should never be silent.

### TradingView

Pine Script is sandboxed with no network or filesystem access, so levels travel as a compact one-line string, copied from the dashboard and pasted into the indicator's settings. The indicator parses it and draws the full set with zone shading, per-level colours and configurable styling. The format is versioned so that older strings still parse against newer indicator builds.

## Engineering notes

```
delayed options chain  ──┐
                         ├──►  collector  ──►  SQLite archive (per-strike map,
live futures quote  ─────┘         │           gamma curve, session history)
                                   │
                                   ├──►  dashboard          (self-contained page)
                                   ├──►  machine-readable snapshot
                                   ├──►  level file    ──►  NinjaTrader 8
                                   └──►  paste string  ──►  TradingView
```

- **Bandwidth decoupled from polling frequency** — every fetch is a conditional request, so an unchanged chain costs a 304 and an empty body instead of several megabytes. Sampling rate became a free parameter
- **Self-checking** — gamma is computed two independent ways and the results cross-validated; any sign disagreement is flagged into the status ledger rather than quietly published
- **Upstream corruption guard** — the source feed rebuilds itself daily and, while it does, briefly publishes a structurally perfect payload with every greek zeroed. JSON validation cannot see this. Coverage is measured on every run and a degraded chain is refused outright, leaving the last good levels in place. The correct output for bad input is no output
- **Honest about age** — the interface headlines the age of the *data*, not the age of the last run. "Computed one minute ago" is true and useless when the source underneath is half an hour stale
- **Concurrent-reader safe** — outputs are written atomically with retry, since a charting platform may hold a file open at the moment it is replaced
- **Timezone-correct scheduling** — the session window is gated on exchange time, so daylight saving never requires a schedule change. Runs unattended on a timer

## Stack

Python (standard library only), SQLite, vanilla JavaScript and hand-built SVG with no framework or build step, C# / NinjaScript, Pine Script.

## Limitations (stated by design)

- The model assumes the **textbook dealer position** (long calls, short puts). It is **not** dealer-calibrated positioning, and the interface states this everywhere a figure is shown
- Translated levels are **zones, not ticks** — a single option strike spans tens of futures points
- Source data is delayed, and open interest is frozen intraday, so the map cannot see positioning built during the current session
- It provides **levels and context, not trade signals** — designed as a context layer beneath an order-flow-driven process, with copy written throughout to never imply otherwise

## Disclaimer

Nothing here is financial advice and no output constitutes a trade signal. Market data is sourced from third-party providers for individual use; redistributing market data commercially requires exchange licensing.
