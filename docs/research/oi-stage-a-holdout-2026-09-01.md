# OI Stage A: the contract-open-interest rule does not survive its own holdout (#459, 2026-09-01)

**Verdict: `NO_CANDIDATE`.** Stage B is not built.

`oi-stage-a-replay-receipt-2026-09-01.json` beside this file is the machine receipt. It is reproduced
by `uv run python notebooks/research/oi_research_cli.py oi-replay` over the sealed corpus
`035cba5f63c30926d9f84d11e22f8b3230011276573d68ba5fb2a0e92517d738`. That command was
`tracefold trading oi-replay` when this receipt was produced; #537 PR-1 moved the replay out of the
service package unchanged, so the same code scores the same corpus to the same digest.

## What was tested

A 36-symbol probe (#459 v3, table C) proposed one rule: Binance **contract** open interest up ≥5% over
an hour while price is up 0–6% over the same hour, on names with ≥$5M open interest, one event per
symbol per day. It reported +266 bps at 4 h across 61 events with permutation p < 0.001.

That probe chose both the rule and the symbols, so Stage A pre-registered the rule unchanged and
replayed it over **every USDT perpetual on Binance** — 525 symbols, 29 days, 4,374,391 five-minute
open-interest readings, 4,345,766 scoreable bars — scoring **only the 490 symbols the probe never
saw**. Entry is the close of the bar the reading opens (so the position is taken up to five minutes
after the reading is observable), hold 4 h, −2% stop against every subsequent low with gaps filled at
the bar's open, 20 bps round-trip cost.

The null is not zero. It is a random entry in the same universe over the same window, exited by the
same rule, drawn 2,000 times at the observed event count.

## What it says

| | N | events/day | 4H net | win | 4H hold (no stop, no cost) | hold win | p |
|---|---|---|---|---|---|---|---|
| **holdout (490 symbols)** | **290** | 10.0 | **−15 bps** | 20.7% | **−58 bps** | 44.1% | **0.518 / 1.000** |
| discovery set (36 symbols) | 50 | 1.7 | +39 bps | 24.0% | **+249 bps** | 56.0% | — |
| every holdout bar (baseline) | 4.0 M | — | −14 bps | — | +9.5 bps | — | — |

Two readings are separated on purpose, because they are different accusations.

**It is not the stop.** Under the probe's own convention — no stop, no cost — the holdout is −58 bps
with p = 1.000: *every* one of 2,000 random draws of 290 same-universe entries did at least as well.

**It is the symbols.** The same rule, same convention, on the 36 symbols it was discovered on still
reads +249 bps and 56.0% — reproducing the original +266 / 55.7% almost exactly. The rule was found on
names selected for having recently carried a provider OI frame, and it does not leave them.

Nothing in the robustness grid rescues it (C60 ≥ 3/5/8%, price bands 0–3 / 3–6 / 6–10% / below zero,
either half of the window: every holdout cell is negative on the hold convention). Neither control
does better: the vendor-style 5-minute dollar move is −17 bps and the ported agent-cli Pulse breakout
is −43 bps, both on the same corpus.

## The one thing that did replicate

The rule selects a real **volatility** regime, just not a directional one. Its entries hit a −2% stop
within four hours **71.7% of the time against a 23.3% base rate**. It finds turbulence reliably and
direction not at all, which is also why the −2% stop that the pre-registration specified turns a −58
bps hold into a −15 bps stopped result: the stop is doing the only useful work in the rule.

## What this closes

- Stage A's gate was N ≥ 200, mean net 4H > 0, win rate > 50%, permutation p < 0.01. Only the first
  passes. Per #459's own terms, `NO_CANDIDATE` closes the issue and Stage B — the five-minute
  `market_oi_snapshots` collector and its SQL candidate generation — is not built.
- Combined with #459 v3's earlier finding that the vendor's five-minute "OI change" is substantially
  price rather than position, the open-interest lane now has **no measured directional edge from
  either the vendor's number or the venue's own**. The frames keep landing and stay auditable; #458
  PR-A already removed the reader notification they used to drive.
- The corpus cannot be re-pulled: Binance serves `openInterestHist` for 30 days. Anyone revisiting
  this has to pull a fresh window, which is why the format seals content-addressed payloads under a
  manifest digest rather than leaving a script's output on a disk somewhere.
