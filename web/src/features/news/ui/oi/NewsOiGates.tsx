import type { TradingGateConfig } from "@features/trading";

import type { NewsOiPolicy } from "../../api/newsQueries";
import { formatCount } from "../../model/newsLabels";
import { oiValueZh } from "../../model/oiSignals";

/**
 * The two independent gates, side by side and never merged.
 *
 * Left: what decides whether a reader is told. Right: what decides whether a Source may be admitted to
 * the capital lane. They read the same frame and answer different questions, and the panel's whole job is
 * keeping that visible.
 *
 * **Neither half shows an Alpha threshold (#331).** The capital policy's numbers are frozen onto each
 * Case and are shown beside the Case that executed them, on 资本判定. A panel that printed today's
 * configuration here invited a reader to measure last week's Case with it — which is exactly what
 * produced 冲突 on rows that had passed.
 */
export function NewsOiGates({
  byRule,
  gate,
  gateUnread,
  policy,
}: {
  byRule: Record<string, number>;
  gate: TradingGateConfig | undefined;
  gateUnread: boolean;
  policy: NewsOiPolicy | null;
}) {
  const windowLabel = compactDuration(policy?.window_ms);
  const changeFloor = policy?.oi_change_at_least_bps ?? 0;
  return (
    <>
      <PolicyPanel
        className="news-oi-policy-news"
        hint="滚动 24h · 决定读者看到什么"
        title="推送闸门 · NEWS.OI"
      >
        <PolicyTile
          label="鲸鱼占比"
          note={`拦下 ${gateCount(byRule.whale_ratio_below_threshold)}`}
          value={policy ? `> ${compactPercent(policy.whale_oi_ratio_above_bps)}` : "—"}
        />
        <PolicyTile
          /*
           * 推送窗口名次, not 窗口名次 (#256). A full window withholds the *push* and says nothing about the
           * move continuing — the note has to carry that, because a reader who read it the other way would
           * treat a withheld frame as an exhausted one.
           */
          label="推送窗口名次"
          note={`拦下 ${gateCount(byRule.beyond_window_rank)} · 满格只拦推送≠衰竭`}
          value={policy ? `≤ ${policy.max_rank_in_window} / ${windowLabel}` : "—"}
        />
        <PolicyTile
          label="OI 变动下限"
          note={`拦下 ${gateCount(byRule.oi_change_below_threshold)}`}
          value={changeFloor > 0 ? `≥${compactPercent(changeFloor)}` : "0（关）"}
        />
      </PolicyPanel>

      <PolicyPanel
        className="news-oi-policy-trading"
        hint={
          gateUnread
            ? "SOURCE_NATIVE · 准入规则未读到"
            : `SOURCE_NATIVE · ${gate?.version ?? "—"} · Alpha 阈值随案例冻结，在资本判定`
        }
        title="准入闸 · TRADING"
      >
        <PolicyTile label="持仓规模" value={gate ? `≥${oiValueZh(gate.min_oi_value_usd)}` : "—"} />
        <PolicyTile label="帧时效" value={gate ? compactDuration(gate.max_age_ms) : "—"} />
        <PolicyTile
          /*
           * #376: source venue selects one code-owned execution binding. There is no venue priority
           * and no cross-venue fallback. `RESEARCH_ONLY` remains only on historical ledger rows.
           *
           * The rank ceiling and the per-symbol cooldown used to sit in this panel and are gone with
           * the gates themselves (#348) — a panel naming a threshold nothing enforces is worse than
           * a shorter panel.
           */
          label="来源绑定"
          note="按来源分区 · 禁止跨场所回退"
          value={sourceBindingLabel(gate?.source_native_bindings)}
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

function PolicyTile({ label, note, value }: { label: string; note?: string; value: string }) {
  return (
    <span className="news-oi-policy-tile">
      <small>{label}</small>
      <b>{value}</b>
      {note ? <em>{note}</em> : null}
    </span>
  );
}

function compactPercent(value: number): string {
  const percent = value / 100;
  return `${Number.isInteger(percent) ? percent.toFixed(0) : percent.toFixed(2)}%`;
}

function compactDuration(value: number | null | undefined): string {
  if (!value) return "窗口";
  const minutes = value / 60_000;
  return minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`;
}

function gateCount(value: number | undefined): string {
  return formatCount(value ?? 0);
}

function sourceBindingLabel(bindings: Record<string, string> | undefined): string {
  if (!bindings) return "—";
  const entries = Object.entries(bindings).sort(([left], [right]) => left.localeCompare(right));
  return entries.length
    ? entries.map(([binding, venue]) => `${venue} → ${binding}`).join(" · ")
    : "—";
}
