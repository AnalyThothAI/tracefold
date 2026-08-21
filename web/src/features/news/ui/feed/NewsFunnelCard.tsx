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
 * Five tiles and no bar. The bar lived here and drew four segments that were differences between layers
 * counted in two different ways (`candidates` by `opened_at_ms`, `triaged` by verdict `created_at_ms`), so at
 * the window edge it read a few percent short and had to be explained. The proportions belong on the status
 * route, where the same numbers get a full-width bar and a sentence naming the biggest drop; here the five
 * counts and one conversion line are the whole answer.
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
      <MetricRow columns={5} label="24 小时漏斗">
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
  const gated = Math.max(0, funnel.received - funnel.candidates);
  const notPushed = Math.max(0, funnel.triaged - funnel.decided_push);
  const ungrounded = Math.max(0, funnel.tagged - funnel.grounded);
  return [
    {
      caption: "收到",
      eyebrow: "RECEIVED",
      note: `1h ${formatCount(funnel.received_1h)}`,
      title: "过去 24 小时从 provider 收到的事件",
      to: newsPath(),
      tone: "plain" as const,
      value: funnel.received,
    },
    {
      caption: "送审",
      eyebrow: "TRIAGED",
      note: percent(funnel.candidates, funnel.received),
      title: gated ? `门禁挡下 ${formatCount(gated)}` : "门禁全部放行",
      to: null,
      tone: "plain" as const,
      value: funnel.candidates,
    },
    {
      caption: "符号落表",
      eyebrow: "GROUNDED",
      // Share of the Events that carried a tag, not of everything received: the denominator has to be the
      // population the number is actually about.
      note: percent(funnel.grounded, funnel.tagged),
      title: ungrounded ? `未落标的表 ${formatCount(ungrounded)}` : "符号全部落表",
      to: null,
      tone: ungrounded ? ("caution" as const) : ("plain" as const),
      value: funnel.grounded,
    },
    {
      caption: "决定推送",
      eyebrow: "DECIDED",
      note: percent(funnel.decided_push, funnel.received),
      title: notPushed ? `模型判不推 ${formatCount(notPushed)}` : "模型全部放行",
      to: `${newsPath()}?outcome=pushed`,
      tone: "plain" as const,
      value: funnel.decided_push,
    },
    {
      caption: "已送达",
      eyebrow: "DELIVERED",
      note: percent(funnel.delivered, funnel.decided_push),
      title: `最近 1 小时 ${formatCount(funnel.delivered_1h)}`,
      to: `${newsPath()}?outcome=pushed`,
      tone: "accent" as const,
      value: funnel.delivered,
    },
  ];
}
