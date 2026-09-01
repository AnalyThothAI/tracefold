# #475 PR-0: OI Runtime owner and concurrency baseline

This is the before-change receipt for Issue #475. It binds the current owner
matrix and reproducible local measurements to exact source
`f495a9fc0d0ba0d528e40b588e76108d80cdfefe`. The machine-readable receipt is
[`oi-runtime-pr0-baseline-2026-09-01.json`](oi-runtime-pr0-baseline-2026-09-01.json).

## Owner matrix

| Fact or action | One current production owner | Authority and repair |
| --- | --- | --- |
| unresolved Signal/Command read | `OiRuntimeDatabaseBridge._cycle` | PostgreSQL indexed anti-join; current production repair is the same 200 ms fixed cycle |
| callback operational state | `OiNautilusStrategy` reading Nautilus Cache/Portfolio | complete private reconciliation repairs Cache outside callbacks |
| authoritative account flat | `_reconcile_account` | complete Binance private position, regular-order and Algo-order reports |
| current Runtime status | `_run_active_runtime` | one generation-fenced current projection plus a 500 ms heartbeat |
| durable audit | `OiRuntimeDatabaseBridge._cycle` | bounded `ExecutionObservationV1` batch and explicit `audit_gap` repair |

The matrix describes the production path, not the desired PR-A/B endpoint. In
particular, `signal_client.run_signal_poll_loop` already implements a second
LISTEN/timeout reader but production does not construct it. PR-A must retain
the indexed query as correctness authority, connect wake to that one owner and
delete the duplicate loop. PR-B must settle the overlap between Nautilus
built-in reconciliation and the custom complete Binance report repair.

## Measured baseline

The opt-in diagnostic uses one migrated isolated PostgreSQL database, starts
the production `OiRuntimeDatabaseBridge` for every input sample, and uses the
pinned Nautilus Strategy/Cache/Portfolio seam. Six samples were run for each
input burst. The harness waits for an explicit completed production cycle,
commits immediately afterward, and timestamps only after the Command and
Signal are actually dequeued. `source_main` is the first parent recorded in
the checked-out HEAD commit, so the scheduled shallow checkout needs no
`origin/main` tracking ref. No cadence or production behavior changed.

| Workload | Result |
| --- | --- |
| 1 event Signal + Command | production fixed-poll persisted through actual dequeue p95 213.165 ms; two indexed reads; zero duplicate pending identities |
| 10 event burst | production fixed-poll p95 184.578 ms through actual dequeue; two indexed reads; zero duplicate pending identities |
| 100 event burst | production fixed-poll p95 203.705 ms through actual dequeue; two indexed reads; zero duplicate pending identities |
| all wake hints discarded | repaired in 203.767 ms by the production Bridge's current 200 ms cadence; 60 s fixture TTL |
| 100 Observation append | 52,990 queued bytes; 32.764 ms append; queue returned to zero |
| entry -> protection -> flatten submit | 2.962 ms pinned lifecycle seam; one entry, one reduce-only stop and one reduce-only exit |
| UI 15-second read window | 29 status reads at 500 ms; p95 26.962 ms |
| process resource envelope | 0.679943 s user CPU, 0.173086 s system CPU, 286,130,176 max RSS bytes |

The optional 525-route capacity case is retained only as a source-derived
subscription-attempt count: current `on_start` calls subscribe once per route,
so 525 synthetic routes mean 525 attempts. It is not an OI collector, a
production route count, an inbound-rate measurement or a business SLO.

## Explicitly not observed

No active Binance Demo Runtime was started in PR-0. Therefore real Nautilus
event-loop lag, inbound Binance market-data rate, complete private
reconciliation count/latency and exchange rate-limit headers are recorded as
`not_observed`, not simulated. Those fields become valid only with active Demo
evidence and are required before PR-F closes the Issue.

Reproduce the complete machine-readable schema, including explicit
`not_observed` provider fields, with:

```text
uv run pytest -q -s tests/integration/test_nautilus_runtime_pr0_baseline.py
```

This diagnostic is scheduled/opt-in and is not fixed-CI merge evidence. The
architecture contract always validates the owner matrix, forbidden collector
boundary and receipt shape.
