import { tradingPath } from "@shared/routing/paths";
import { Link } from "react-router-dom";

import { formatCount } from "../../model/newsLabels";
import { OI_PARSE_FAILED_RULE, OI_STORED_RULE } from "../../model/oiSignals";

/**
 * What this lane does with a frame: it parses one and stores it.
 *
 * Since #458 News has no notification rule of its own, and since #528 PR-2 it no longer prints the Signal
 * lane's admission thresholds either — those moved to the desk's funnel, beside the admission answers they
 * filed, which is the only place they explain anything. This page keeps the question it can answer alone.
 *
 * **No Alpha threshold, on either side.** The Alpha policy's numbers are frozen onto each Case and are
 * shown beside the Case that executed them. A panel that printed today's configuration here invited a
 * reader to measure last week's Case with it — which is exactly what produced 冲突 on rows that had passed.
 */
export function NewsOiGates({ byRule }: { byRule: Record<string, number> }) {
  return (
    <article className="news-oi-policy news-oi-policy-news">
      <header>
        <h2>来源入账 · NEWS.OI</h2>
        <small>滚动 24h · 不再决定读者看到什么</small>
      </header>
      <div className="news-oi-policy-tiles">
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
      </div>
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

function gateCount(value: number | undefined): string {
  return formatCount(value ?? 0);
}
