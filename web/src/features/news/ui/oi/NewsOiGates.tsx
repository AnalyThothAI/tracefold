import type { TradingGateConfig, TradingStrategyConfig } from "@features/trading";

import type { NewsOiPolicy, NewsOiTradeFloors } from "../../api/newsQueries";
import { formatCount } from "../../model/newsLabels";
import { oiValueZh } from "../../model/oiSignals";

/**
 * The two independent policy sets, condensed to the approved at-a-glance comparison.
 *
 * The capital half is the Candidate Gate's, not the operator's settings document (#269). After #264 the
 * lane has exactly one admission owner and `trading.candidates.min_oi_value_usd` is only one of its
 * inputs; the console was printing the settings figure — 2000 万 — while admission actually ran at 500 万,
 * and 鲸鱼盈利 ≥95% was a *strategy* threshold shown as though it were the lane's. Alpha thresholds
 * belong to whichever versioned strategy answers a Case and are not a property of the lane, so this
 * panel names the admission rules and links out rather than picking one strategy's numbers to display.
 */
export function NewsOiGates({
  byRule,
  floors,
  gate,
  gateUnread,
  policy,
  strategies,
}: {
  byRule: Record<string, number>;
  floors: NewsOiTradeFloors;
  gate: TradingGateConfig | undefined;
  gateUnread: boolean;
  policy: NewsOiPolicy | null;
  strategies: readonly TradingStrategyConfig[];
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
        /*
         * The strategy count rides in the hint rather than as a fifth tile: the tile row is a fixed
         * four-column band of a fixed height, and a fifth wrapped into a second row the panel clips.
         * Naming where the Alpha floors live is the whole job here anyway — the numbers themselves are
         * per-strategy and belong beside the case that a strategy decided, on 杠杆异动.
         */
        hint={
          gateUnread
            ? `${floors.execution_environment} · 准入规则未读到`
            : `${floors.execution_environment} · ${floors.enabled ? "已启用" : "资本通道关闭"}${
                strategies.length ? ` · Alpha 地板在 ${strategies.length} 条策略各自` : ""
              }`
        }
        title="准入闸 · TRADING"
      >
        <PolicyTile label="持仓规模" value={gate ? `≥${oiValueZh(gate.min_oi_value_usd)}` : "—"} />
        <PolicyTile
          label="交易窗口名次"
          note="与推送名次同名不同闸"
          value={gate ? `≤ ${gate.max_rank_in_window}` : "—"}
        />
        <PolicyTile label="帧时效" value={gate ? compactDuration(gate.max_age_ms) : "—"} />
        <PolicyTile
          label="可路由场所"
          note={gate ? `冷却 ${compactDuration(gate.symbol_cooldown_ms)}` : undefined}
          value={gate?.venue_priority?.length ? gate.venue_priority.join(" / ") : "—"}
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
