import { newsPath } from "@shared/routing/paths";
import { Card } from "@shared/ui/Card";
import { Metric, MetricRow } from "@shared/ui/Metric";

import type { NewsFunnel, NewsStatus } from "../../api/newsQueries";
import { formatCount, percent } from "../../model/newsLabels";

import "./newsFunnel.css";

/**
 * Where the last 24 hours went, above the feed. Every figure is either a `funnel_24h` field or the difference
 * between two of them — the browser reports where the Events went, it does not decide it.
 *
 * Four durable stages and no bar: Event intake, Gate admission, judgment and delivery.
 */
export function NewsFunnelCard({ status }: { status?: NewsStatus }) {
  const funnel = status?.funnel_24h;
  if (!funnel) return null;
  const failed = status.delivery.terminal_24h;
  // #87: how many Events *offered* a coin tag and had none of them land. Measured against `tagged`, never
  // against `triaged`: a macro headline that named no asset did not fail to resolve a symbol, and counting
  // it here would report a fault in the instrument table that does not exist.
  const ungrounded = Math.max(0, funnel.tagged - funnel.grounded);
  return (
    <Card
      aria-label="过去 24 小时漏斗"
      className="news-funnel-card"
      flush
      hint={
        <span className="news-funnel-summary">
          转化 <b>{percent(funnel.delivered, funnel.received)}</b>
          {ungrounded ? (
            <>
              {" · 符号未落表拦下 "}
              <b data-tone="caution">{formatCount(ungrounded)}</b>
            </>
          ) : failed ? (
            <>
              {" · 投递失败 "}
              <b data-tone="alert">{formatCount(failed)}</b>
            </>
          ) : (
            " · 无投递失败"
          )}
        </span>
      }
      title="Last 24h"
      titleStyle="eyebrow"
    >
      <MetricRow columns={4} label="24 小时漏斗">
        {funnelTiles(funnel).map((tile) => (
          <Metric
            caption={tile.caption}
            eyebrow={tile.eyebrow}
            key={tile.eyebrow}
            note={tile.note}
            title={tile.title}
            to={tile.to}
            tone={tile.tone}
            value={formatCount(tile.value)}
          />
        ))}
      </MetricRow>
    </Card>
  );
}

function funnelTiles(funnel: NewsFunnel) {
  const gated = Math.max(0, funnel.received - funnel.admitted);
  const unjudged = Math.max(0, funnel.admitted - funnel.triaged);
  return [
    {
      caption: "采集",
      eyebrow: "RECEIVED",
      note: "",
      title: "过去 24 小时从 provider 收到的事件",
      to: `${newsPath()}?outcome=all&hours=24`,
      tone: "plain" as const,
      value: funnel.received,
    },
    {
      caption: "过门禁",
      eyebrow: "ADMITTED",
      note: percent(funnel.admitted, funnel.received),
      title: gated ? `门禁挡下 ${formatCount(gated)}` : "门禁全部放行",
      to: null,
      tone: "plain" as const,
      value: funnel.admitted,
    },
    {
      caption: "已审稿",
      eyebrow: "JUDGED",
      note: percent(funnel.triaged, funnel.admitted),
      title: unjudged ? `尚待审稿 ${formatCount(unjudged)}` : "已全部审稿",
      to: null,
      tone: "plain" as const,
      value: funnel.triaged,
    },
    {
      caption: "已推送",
      eyebrow: "PUSHED",
      note: percent(funnel.delivered, funnel.received),
      title: `最近 1 小时 ${formatCount(funnel.delivered_1h)}`,
      to: `${newsPath()}?outcome=pushed&hours=24`,
      tone: "accent" as const,
      value: funnel.delivered,
    },
  ];
}
