# Wallet net-buy cutover (#641)

This is a stopped-writer change from schema 20260908_0375 to 20260912_0376.
It does not authorize enabling wallet notifications, deleting production history, or
changing/restarting the order runtime. Record implementation, merge, deployment and
notification restoration separately in Issue #641.

## Before the maintenance window

1. Identify the operator-owned paths with `uv run tracefold config`; keep secrets out
   of logs and issue comments. Inventory every process/container reading that same
   configuration, including Serve, Workers, maintenance commands and any order runtime.
   Record names, immutable image IDs/source SHAs, config paths and intended disposition.
   A historical deployment receipt is not a current inventory.
2. Require the fixed full CI plan and successful `ci-gate` for the exact final main SHA.
   Verify a database backup and the existing restore procedure. Measure wallet relation
   sizes and available space: the archive briefly duplicates old event/check/outcome/state
   rows. Migration lock timeout is 5s and statement timeout is 60s.
3. Review the four engineering defaults: 5m N=3, 30m N=5, per-wallet net USD=1000,
   trigger age=60s. N must be at least 2. Old customized buy/exit/crowding settings have
   no equivalent conversion. Reset the old 600s age explicitly.
4. Run the offline tool against a copy of the operator config:

```sh
uv run python scripts/migrate_wallet_net_buy_config.py --config /operator/config.yaml
uv run python scripts/migrate_wallet_net_buy_config.py --config /operator/config.yaml --output /operator/config.net-buy.yaml
```

Optional `--fast-n`, `--slow-n`, `--min-net-buy-usd`, `--trigger-max-age-s` specify
reviewed new values. Repeating the same output is a no-op; different existing output
is refused. The source is never overwritten. The output contains secrets because it is
a full config: protect it exactly like the source. Only rule values and removed key
names are printed. Unknown keys/invalid settings fail without dumping input values.
The current runtime loader performs no conversion.

Removed rule keys: `buy_min_usd`, `buy_window_s`, `exit_notifications_enabled`,
`exit_ratio_bps`, `exit_min_position_usd`, `exit_cascade_window_s`,
`exit_cascade_min_usd`, `crowding_n`, `crowding_window_s`, `crowding_min_usd`,
`crowding_premium_late_bps`; the entire `news.chain_tape.digest` mapping is removed.
Existing collector, roster, polling, retention, enabled and notification switches remain.

## Coordinated cut

Stop the identified wallet/Serve writers and shared-config readers according to the
existing release procedure. An old Settings consumer must not race the config change.
If the order runtime shares that config, coordinate an independently authorized
compatible image/config handoff; do not restart it implicitly.

Apply the forward migration with the existing database lifecycle command, then publish
the reviewed config and start the matching Serve/Workers image. Record the exact image
and schema revision. The migration atomically:

- copies every retired wallet event, check, outcome and tape-state row using
  `to_jsonb` into `news_market_wallet_archive`;
- retains frozen channel payloads, attempt/receipt clocks and sent/unknown evidence;
- terminates old unattempted pending/unavailable intents with
  `wallet_net_buy_cutover`, completes old derivation candidates, and excludes retired
  attempted pending deliveries from the new sender;
- replaces current event/outcome tables with the single episode contract, records a
  cutover timestamp and resets unsupported monitoring coverage.

No old crowding row is renamed to net buy, and no old mark is promoted to a return
baseline. A failure rolls the entire migration back. Downgrade is refused; roll forward
or use the verified backup restore under the same stopped-writer boundary.

## Receipt after startup

Verify `chain_tape`, `wallet_net_buy`, `wallet_prices` capabilities and absence of the
retired research/digest task names. Compare archive record counts and sampled/full JSON
against the backup, and compare frozen attempted/sent/unknown deliveries without
re-rendering them. Confirm no old pending candidate is adopted.

Inspect scanned chain time, block/log cutoff, coverage start and gaps. Newly monitored
members must accumulate complete 5m/30m support before counting. Unknown receipt/reorg
state must remain visible and must not produce an affirmative complete-coverage alert.

Open the event list and a direct episode deep link on desktop and mobile. Check initial
and current snapshots separately, exact amounts, sell/transfer exclusions, auxiliary
status failure, paging and unknown/late price labels. Inspect a fresh synthetic/replay
notification through the existing Feishu/Telegram serializers; never send a production
test message without authorization.

Record chain→receive→detect→intent→first-attempt clocks from actual rows. State the
sample size, source interval, known-at roster support, missing pricing/coverage counts (price samples have a 60-second maximum delay),
number of episodes, suppressed reasons, deduplication and actual price comparability.
Do not report synthetic replay counts as live signal frequency, profitability or latency
SLO evidence. Missing historical roster support means that historical period is
unevaluable, not evidence of zero signals.

Wallet notifications remain at the operator's previous setting. Restoration is a
separate decision and receipt; only subsequently opened episodes may create a first
intent. The code/CI receipt alone is not a deployment or restoration receipt.
