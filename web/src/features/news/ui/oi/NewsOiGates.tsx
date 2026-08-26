import type { NewsOiPolicy, NewsOiTradeFloors } from "../../api/newsQueries";
import { formatCount } from "../../model/newsLabels";
import { oiValueZh } from "../../model/oiSignals";

/** The two independent policy sets, condensed to the approved at-a-glance comparison. */
export function NewsOiGates({
  byRule,
  floors,
  policy,
}: {
  byRule: Record<string, number>;
  floors: NewsOiTradeFloors;
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
          label="窗口名次"
          note={`拦下 ${gateCount(byRule.beyond_window_rank)}`}
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
          floors.enabled
            ? `${floors.mode.toUpperCase()} · 已启用 · 四条另判`
            : `${floors.mode.toUpperCase()} · 资本通道关闭`
        }
        title="交易地板 · TRADING"
      >
        <PolicyTile
          label="鲸鱼盈利"
          value={`≥${compactPercent(floors.min_whale_long_profit_bps)}`}
        />
        <PolicyTile label="持仓规模" value={`≥${oiValueZh(floors.min_oi_value_usd)}`} />
        <PolicyTile
          label="已走行情带"
          value={`${compactPercent(floors.min_price_move_bps)}–${compactPercent(floors.max_price_move_bps)} / 1h`}
        />
        <PolicyTile label="方向" value={floors.allow_short ? "多 / 空" : "只多"} />
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
