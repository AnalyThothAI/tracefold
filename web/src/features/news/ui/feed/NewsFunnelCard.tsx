import { newsPath } from "@shared/routing/paths";
import { Link } from "react-router-dom";

import type { NewsFunnel, NewsStatus } from "../../api/newsQueries";
import { formatCount, percent } from "../../model/newsLabels";

import "./newsFunnel.css";

type Tile = {
  accent?: boolean;
  hint: string;
  label: string;
  pct: string;
  to: string | null;
  value: number;
};

/**
 * The 24 h funnel above the feed. Every figure is either a server field from `funnel_24h` or the difference
 * between two of them — the browser reports where the Events went, it does not decide it.
 */
export function NewsFunnelCard({ status }: { status?: NewsStatus }) {
  const funnel = status?.funnel_24h;
  if (!funnel) return null;
  const tiles = funnelTiles(funnel);
  const failed = status.delivery.terminal_24h;
  // #87: how many Events named an asset the venue catalogues do not have. It displaces the delivery-failure
  // note when it is non-zero because it is the larger recall problem — a card that never existed cannot fail
  // to send.
  const ungrounded = Math.max(0, funnel.triaged - funnel.grounded);
  return (
    <section aria-label="过去 24 小时漏斗" className="news-funnel-card">
      <div className="news-funnel-card-head">
        <span className="news-funnel-eyebrow">Last 24h</span>
        <span className="news-funnel-summary">
          转化 <b>{percent(funnel.delivered, funnel.received)}</b> · 拦下{" "}
          <b>{formatCount(Math.max(0, funnel.received - funnel.delivered))}</b> 条 ·{" "}
          {ungrounded ? (
            <>
              符号未落表 <b data-tone="caution">{formatCount(ungrounded)}</b> 条
            </>
          ) : failed ? (
            <>
              投递失败 <b>{formatCount(failed)}</b> 条
            </>
          ) : (
            "无投递失败"
          )}
        </span>
      </div>
      <div aria-hidden className="news-funnel-proportion">
        {proportionSegments(funnel).map((segment) => (
          <span
            data-layer={segment.layer}
            key={segment.layer}
            style={{ width: `${segment.share}%` }}
          />
        ))}
      </div>
      <ol className="news-funnel-tiles">
        {tiles.map((tile) => (
          <li key={tile.label}>
            <FunnelTile tile={tile} />
          </li>
        ))}
      </ol>
    </section>
  );
}

function FunnelTile({ tile }: { tile: Tile }) {
  const body = (
    <>
      <span className="news-funnel-tile-label">
        {tile.label}
        {tile.pct ? <small>{tile.pct}</small> : null}
      </span>
      <b>{formatCount(tile.value)}</b>
      <small>{tile.hint}</small>
    </>
  );
  if (!tile.to) {
    return (
      <span className="news-funnel-tile" data-accent={tile.accent || undefined}>
        {body}
      </span>
    );
  }
  return (
    <Link className="news-funnel-tile" data-accent={tile.accent || undefined} to={tile.to}>
      {body}
    </Link>
  );
}

function funnelTiles(funnel: NewsFunnel): Tile[] {
  const gated = Math.max(0, funnel.received - funnel.candidates);
  const notPushed = Math.max(0, funnel.triaged - funnel.decided_push);
  const ungroundedCount = Math.max(0, funnel.triaged - funnel.grounded);
  return [
    {
      hint: `最近 1 小时 ${formatCount(funnel.received_1h)}`,
      label: "收到",
      pct: "",
      to: newsPath(),
      value: funnel.received,
    },
    {
      hint: gated ? `门禁挡下 ${formatCount(gated)}` : "门禁全部放行",
      label: "送审",
      pct: percent(funnel.candidates, funnel.received),
      to: null,
      value: funnel.candidates,
    },
    {
      hint: ungroundedCount ? `未落标的表 ${formatCount(ungroundedCount)}` : "符号全部落表",
      label: "符号落表",
      pct: percent(funnel.grounded, funnel.received),
      to: null,
      value: funnel.grounded,
    },
    {
      hint: notPushed ? `模型判不推 ${formatCount(notPushed)}` : "模型全部放行",
      label: "决定推送",
      pct: percent(funnel.decided_push, funnel.received),
      to: `${newsPath()}?outcome=pushed`,
      value: funnel.decided_push,
    },
    {
      accent: true,
      hint: `最近 1 小时 ${formatCount(funnel.delivered_1h)}`,
      label: "已送达",
      pct: percent(funnel.delivered, funnel.decided_push),
      to: `${newsPath()}?outcome=pushed`,
      value: funnel.delivered,
    },
  ];
}

/**
 * The bar reads left to right as "gated / not triaged / judged-but-held / delivered". The four shares are
 * differences between adjacent layers, so they sum to `received` and the bar is always full.
 */
function proportionSegments(funnel: NewsFunnel) {
  const total = Math.max(1, funnel.received);
  const layers = [
    { layer: "gated", value: funnel.received - funnel.candidates },
    { layer: "untriaged", value: funnel.candidates - funnel.triaged },
    { layer: "held", value: funnel.triaged - funnel.delivered },
    { layer: "delivered", value: funnel.delivered },
  ];
  // Grounding is a property of the Events in the `held`/`delivered` bands, not a band of its own: an Event
  // whose symbols never landed can still have been pushed. It gets a tile, deliberately not a segment.
  return layers.map((layer) => ({
    layer: layer.layer,
    share: (Math.max(0, layer.value) / total) * 100,
  }));
}
