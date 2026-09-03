import { Card } from "@shared/ui/Card";

import type { TradingCases, TradingGate, TradingGateConfig } from "../api/tradingQueries";
import { caseFigures, caseReasonRows } from "../model/tradingCases";
import { GATE_STATUS_ZH, caseClock, gateReasonLabel } from "../model/tradingLabels";

/**
 * Block 5: the 24 h funnel — what admission answered, what the policy then decided, and the rules both ran.
 *
 * Every figure is a durable count the server aggregated over the same window; nothing here is counted from
 * the rows on screen, which are one page of it. The admission configuration moved here from `/news/oi`
 * (#528 PR-2): it is the Signal lane's own threshold set, it arrives in the same `/api/trading/gate` batch
 * as the answers it filed, and printing it beside those answers is the only place it explains anything.
 */
export function TradingFunnel({
  cases,
  casesFailed,
  gate,
  gateFailed,
}: {
  cases: TradingCases | undefined;
  casesFailed: boolean;
  gate: TradingGate | undefined;
  gateFailed: boolean;
}) {
  const statusRows = countRows(gate?.status_counts_24h);
  const gateReasons = countRows(gate?.reason_counts_24h);
  return (
    <div className="trading-funnel-grid">
      <Card
        hint={
          gateFailed
            ? "准入台账本轮读取失败"
            : `最新来源 ${caseClock(gate?.latest_source_at_ms)} · 最近放行 ${caseClock(gate?.latest_gate_eligible_at_ms)}`
        }
        title="来源准入 · 24h"
      >
        <CountList
          empty={gateFailed ? "准入台账读取失败，不能据此断言为空。" : "本窗口没有准入判定。"}
          label={(key) => GATE_STATUS_ZH[key] ?? key}
          rows={statusRows}
        />
        <CountList
          empty={gateFailed ? "" : "本窗口没有具名的准入原因。"}
          label={(key) => `${gateReasonLabel(key)} · ${key}`}
          rows={gateReasons}
        />
      </Card>

      <Card
        hint={casesFailed ? "Case 账本本轮读取失败" : "冻结判定的终局与原因"}
        title="Alpha 成案 · 24h"
      >
        <div className="trading-fact-grid">
          {caseFigures(cases).map((figure) => (
            <span
              className="trading-fact"
              data-tone={figure.tone === "plain" ? undefined : figure.tone}
              key={figure.key}
            >
              <small>{figure.label}</small>
              <b>{figure.value}</b>
            </span>
          ))}
        </div>
        <CountList
          empty={
            casesFailed ? "Case 账本读取失败，不能据此断言为空。" : "本窗口没有具名的判定原因。"
          }
          label={(key) => key}
          rows={caseReasonRows(cases)}
        />
      </Card>

      <Card
        hint={
          gate
            ? `SOURCE_NATIVE · ${gate.config.version} · ${gate.config.config_digest.slice(0, 12)}`
            : "SOURCE_NATIVE · 准入规则未读到"
        }
        title="准入闸 · TRADING"
      >
        <div className="trading-fact-grid">
          <span className="trading-fact">
            <small>持仓规模</small>
            <b>{gate ? `≥${oiValueLabel(gate.config.min_oi_value_usd)}` : "—"}</b>
          </span>
          <span className="trading-fact">
            <small>帧时效</small>
            <b>{gate ? compactDuration(gate.config.max_age_ms) : "—"}</b>
          </span>
          <span className="trading-fact">
            {/*
             * Source venue names evidence provenance only: there is no venue priority, no cross-venue
             * fallback, and no venue-derived execution route.
             */}
            <small>来源场所 · 只决定证据来源</small>
            <b>{sourceVenueLabel(gate?.config)}</b>
          </span>
        </div>
        {/*
         * No Alpha threshold here, ever. Those are frozen onto each Case and are shown beside the Case
         * that executed them in block 6 — a panel printing today's configuration invited a reader to
         * measure last week's Case with it, which is exactly what produced 冲突 on rows that had passed.
         */}
        <p className="trading-inline-empty">Alpha 阈值随案例冻结，见下方 Case 的冻结判定证据。</p>
      </Card>
    </div>
  );
}

function CountList({
  empty,
  label,
  rows,
}: {
  empty: string;
  label: (key: string) => string;
  rows: Array<[string, number]>;
}) {
  if (!rows.length) return empty ? <p className="trading-inline-empty">{empty}</p> : null;
  return (
    <div className="trading-count-list">
      {rows.map(([key, count]) => (
        <span className="trading-count-row" key={key}>
          <small>{label(key)}</small>
          <b>{count}</b>
        </span>
      ))}
    </div>
  );
}

function countRows(counts: Record<string, number> | undefined): Array<[string, number]> {
  return Object.entries(counts ?? {}).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function compactDuration(value: number | null | undefined): string {
  if (!value) return "窗口";
  const minutes = value / 60_000;
  return minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`;
}

/** `≥500 万` — the floor in the unit an operator reads it in, never a raw 5000000. */
function oiValueLabel(value: number): string {
  if (value >= 100_000_000) return `${Math.round(value / 100_000_000)} 亿`;
  return `${Math.round(value / 10_000)} 万`;
}

function sourceVenueLabel(config: TradingGateConfig | undefined): string {
  const venues = config?.source_venues ?? [];
  return venues.length ? [...venues].sort().join(" · ") : "—";
}
