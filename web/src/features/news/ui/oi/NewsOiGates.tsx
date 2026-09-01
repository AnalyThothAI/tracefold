import type { TradingGateConfig } from "@features/trading";
import { tradingPath } from "@shared/routing/paths";
import { Link } from "react-router-dom";

import { formatCount } from "../../model/newsLabels";
import { OI_PARSE_FAILED_RULE, OI_STORED_RULE, oiValueZh } from "../../model/oiSignals";

/**
 * What this lane does with a frame, beside what may reach the Signal lane.
 *
 * Left: News, which since #458 has no notification rule of its own — it parses a frame and stores it.
 * Right: what decides whether a Source may reach the Signal lane. The left panel used to hold three
 * thresholds of its own and is deliberately not replaced by a shorter set of them: over 48 h the two
 * halves selected disjoint sets of frames, and the fix was to stop having two teachers, not to retune one.
 *
 * **Neither half shows an Alpha threshold.** The Alpha policy's numbers are frozen onto each Case and are
 * shown beside the Case that executed them, on Alpha 判定. A panel that printed today's
 * configuration here invited a reader to measure last week's Case with it — which is exactly what
 * produced 冲突 on rows that had passed.
 */
export function NewsOiGates({
  byRule,
  gate,
  gateUnread,
}: {
  byRule: Record<string, number>;
  gate: TradingGateConfig | undefined;
  gateUnread: boolean;
}) {
  return (
    <>
      <PolicyPanel
        className="news-oi-policy-news"
        hint="滚动 24h · 不再决定读者看到什么"
        title="来源入账 · NEWS.OI"
      >
        <PolicyTile
          label="已入账"
          note="24h 解析成功的帧"
          value={gateCount(byRule[OI_STORED_RULE])}
        />
        <PolicyTile
          label="解析失败"
          note="模板变了才会涨"
          value={gateCount(byRule[OI_PARSE_FAILED_RULE])}
        />
        <PolicyTile
          /* The third tile is where the push gate used to be, and it now names who owns that decision. */
          label="推送"
          note="433-E 通电前为零"
          value={<Link to={tradingPath()}>Signal 通道 →</Link>}
        />
      </PolicyPanel>

      <PolicyPanel
        className="news-oi-policy-trading"
        hint={
          gateUnread
            ? "SOURCE_NATIVE · 准入规则未读到"
            : `SOURCE_NATIVE · ${gate?.version ?? "—"} · Alpha 阈值随案例冻结，在 Alpha 判定`
        }
        title="准入闸 · TRADING"
      >
        <PolicyTile label="持仓规模" value={gate ? `≥${oiValueZh(gate.min_oi_value_usd)}` : "—"} />
        <PolicyTile label="帧时效" value={gate ? compactDuration(gate.max_age_ms) : "—"} />
        <PolicyTile
          /*
           * #376: source venue selects one code-owned execution binding. There is no venue priority
           * and no cross-venue fallback. `RESEARCH_ONLY` was a fifth gate status for a venue that
           * could be studied and not traded; no row ever carried it, and #460 removed the name.
           *
           * The rank ceiling and the per-symbol cooldown used to sit in this panel and are gone with
           * the gates themselves (#348) — a panel naming a threshold nothing enforces is worse than
           * a shorter panel.
           */
          label="来源场所"
          note="只决定证据来源，不决定执行路由"
          value={sourceVenueLabel(gate?.source_venues)}
        />
      </PolicyPanel>
    </>
  );
}

function PolicyPanel({
  children,
  className,
  hint,
  title,
}: {
  children: React.ReactNode;
  className: string;
  hint: string;
  title: string;
}) {
  return (
    <article className={`news-oi-policy ${className}`}>
      <header>
        <h2>{title}</h2>
        <small>{hint}</small>
      </header>
      <div className="news-oi-policy-tiles">{children}</div>
    </article>
  );
}

function PolicyTile({
  label,
  note,
  value,
}: {
  label: string;
  note?: string;
  value: React.ReactNode;
}) {
  return (
    <span className="news-oi-policy-tile">
      <small>{label}</small>
      <b>{value}</b>
      {note ? <em>{note}</em> : null}
    </span>
  );
}

function compactDuration(value: number | null | undefined): string {
  if (!value) return "窗口";
  const minutes = value / 60_000;
  return minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`;
}

function gateCount(value: number | undefined): string {
  return formatCount(value ?? 0);
}

function sourceVenueLabel(venues: string[] | undefined): string {
  return venues?.length ? [...venues].sort().join(" · ") : "—";
}
