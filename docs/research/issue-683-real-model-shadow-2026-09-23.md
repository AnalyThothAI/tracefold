# Issue 683 real-model shadow diagnostic (2026-09-23)

This is a local, read-only integration receipt for the model seam. The source
sample came from one stored OI fact and one stored pushed catalyst verdict in the
existing News ledger. Each was paired with current public Binance market data
through `FrameReader`, sent once to the configured `openai/qwen3.8-27b` route
through the real DSPy `TradeAnalyst`, and compiled with the strict pure decision
compiler. The diagnostic wrote no production Case, Signal, TradePlan, order or
configuration. The exact model input, raw request/response and market frames
were kept in a temporary local content-addressed archive during the run because
they contain source text; that archive is not part of this repository and is no
longer available after the test environment restarted.

| Input | Provider | Agent action | Compiled score coverage | Tokens in/out | Reported cost |
| --- | --- | --- | --- | --- | --- |
| OI, PENDLE | success | NO_TRADE | 9000 bps each direction; partial score stays null | 2965 / 1341 | null |
| Catalyst, POL | success | WATCH | 6500 bps each direction; partial score stays null | 2956 / 1515 | null |

The [sanitized machine receipt](issue-683-real-model-shadow-2026-09-23.json)
records the source identity hash, brief/prompt and artifact digests, model,
physical usage, factor weights, factor references and compiled action. Both
responses validated; neither was forced to TRADE. `null` cost means the route
did not report a charge, not that the call was free. The checked-in receipt
contains no original headline, raw provider payload or credential.

This exercise proves the real model adapter accepts both input kinds and its
output passes the same compiler as online Analysis. It is not evidence of a
deployed Analysis process, live orders, venue fills or strategy profitability.
