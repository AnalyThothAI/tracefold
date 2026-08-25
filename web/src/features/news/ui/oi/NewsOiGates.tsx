import { Card } from "@shared/ui/Card";
import { ThresholdIcon, WhaleShareIcon, WindowClockIcon } from "@shared/ui/icons";

import type { NewsOiPolicy, NewsOiTradeFloors } from "../../api/newsQueries";
import { formatCount } from "../../model/newsLabels";
import { oiPercent, oiRuleLabel, oiValueZh, oiWindowLabel } from "../../model/oiSignals";
import { NewsSourceLine } from "../chrome/NewsSourceLine";


/**
 * The two threshold sets, side by side and never merged (#207 principle 4).
 *
 * They answer different questions about the same frame. The News gates decide whether a reader is told; the
 * capital lane's floors decide whether it may open exposure. Clearing one is no evidence about the other, so
 * they get separate panels, separate headings, and — where the capital lane is switched off — a sentence
 * saying so, because a floor nothing is currently applying is a published band rather than a gate.
 */
export function NewsOiGates({
  byRule,
  floors,
  policy,
}: {
  byRule: Record<string, number>;
  floors: NewsOiTradeFloors;
  policy: NewsOiPolicy | null;
}) {
  const windowLabel = oiWindowLabel(policy?.window_ms);
  const changeFloorOn = (policy?.oi_change_at_least_bps ?? 0) > 0;
  return (
    <>
      <Card flush hint="news.oi · 改配置不用发版" title="新闻闸门与它们拦下的量">
        <div className="news-oi-gate-rows">
          <GateRow
            count={byRule.whale_ratio_below_threshold}
            icon={<WhaleShareIcon aria-hidden />}
            rule="whale_ratio_below_threshold"
            threshold={policy ? `> ${oiPercent(policy.whale_oi_ratio_above_bps)}` : "—"}
            title="鲸鱼占比要大于阈值"
          />
          <GateRow
            count={byRule.beyond_window_rank}
            icon={<WindowClockIcon aria-hidden />}
            rule="beyond_window_rank"
            threshold={policy ? `前 ${policy.max_rank_in_window} 次` : "—"}
            title={windowLabel ? `${windowLabel}窗口内只放前几次` : "窗口内只放前几次"}
          />
          <GateRow
            count={byRule.oi_change_below_threshold}
            disabled={!changeFloorOn}
            icon={<ThresholdIcon aria-hidden />}
            rule="oi_change_below_threshold"
            threshold={
              changeFloorOn && policy ? `≥ ${oiPercent(policy.oi_change_at_least_bps)}` : "未启用"
            }
            title="持仓变动下限"
          />
        </div>
        <NewsSourceLine
          note="拦下量按判定痕迹里的闸门名分组；pipeline.dropped_by_rule 记的是 admission，分不出闸门"
          path="GET /api/news/status → oi.policy · oi.by_rule_24h"
        />
      </Card>

      <Card
        flush
        hint={floors.enabled ? `trading · ${floors.mode}` : "trading 未启用"}
        title="交易地板（另一套阈值）"
      >
        <div className="news-oi-gate-rows">
          <FloorRow
            title="鲸鱼多头盈利"
            value={`≥ ${oiPercent(floors.min_whale_long_profit_bps)}`}
            why="研究里唯一均值为正的分桶"
          />
          <FloorRow
            title="持仓规模"
            value={`≥ ${oiValueZh(floors.min_oi_value_usd)}`}
            why="低于此处的分桶实测最差"
          />
          <FloorRow
            missing
            title="帧前 1H 已走"
            value={`${oiPercent(floors.min_price_move_bps)} – ${oiPercent(floors.max_price_move_bps)}`}
            why="News 价格面只锚定事件后的 p0/p1/p4，帧前一小时的价格没有落表，本页无法逐帧判定"
          />
        </div>
        <NewsSourceLine
          note={
            floors.enabled
              ? "推送 ≠ 可交易：上面的闸门决定读者是否收到，这里的地板决定资本是否开仓"
              : "trading 当前关闭，这三条只是已发表的研究带，没有任何一条正在生效"
          }
          path="GET /api/news/status → oi.trade_floors"
        />
      </Card>
    </>
  );
}

function GateRow({
  count,
  disabled = false,
  icon,
  rule,
  threshold,
  title,
}: {
  count?: number;
  disabled?: boolean;
  icon: React.ReactNode;
  rule: string;
  threshold: string;
  title: string;
}) {
  return (
    <article className="news-oi-gate-row" data-disabled={disabled || undefined}>
      <span className="news-oi-gate-icon">{icon}</span>
      <span className="news-oi-gate-copy">
        <b>{title}</b>
        {/* The key is the server's; the Chinese beside it is this console's reading of it, never a rename. */}
        <small>
          <code>{rule}</code> · {oiRuleLabel(rule)}
        </small>
      </span>
      <b className="news-oi-gate-threshold">{threshold}</b>
      <span className="news-oi-gate-count">{count == null ? "—" : formatCount(count)}</span>
    </article>
  );
}

function FloorRow({
  missing = false,
  title,
  value,
  why,
}: {
  missing?: boolean;
  title: string;
  value: string;
  why: string;
}) {
  return (
    <article className="news-oi-gate-row" data-missing={missing || undefined}>
      <span className="news-oi-gate-copy news-oi-gate-copy-wide">
        <b>{title}</b>
        <small>{why}</small>
      </span>
      <b className="news-oi-gate-threshold">{value}</b>
      <span className="news-oi-gate-count">{missing ? "未测量" : ""}</span>
    </article>
  );
}
