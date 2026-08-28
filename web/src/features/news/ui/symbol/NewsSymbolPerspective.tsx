import { STRATEGY_ZH } from "@features/trading";
import { Card } from "@shared/ui/Card";

import { displayTime } from "../../model/newsLabels";
import type {
  SymbolBandPlacement,
  SymbolFloorRow,
  SymbolPerspective,
} from "../../model/symbolPerspective";
import { NewsEmptyNote } from "../chrome/NewsChrome";
import { NewsSourceLine } from "../chrome/NewsSourceLine";

import "./newsSymbolPerspective.css";

/**
 * Four placements, four sentences. Below the floor and above the ceiling are opposite failures — one is
 * "nothing has happened yet", the other is the chasing bucket — and one sentence for both said 追涨 over a
 * frame whose price had gone down.
 */
/** Within a tenth of either end the marker's centred label would hang off the card; anchor it inward. */
function edgeOf(percent: number): "start" | "end" | undefined {
  if (percent < 10) return "start";
  if (percent > 90) return "end";
  return undefined;
}

/**
 * One word per verdict, and 未冻结 only for the verdict that means it.
 *
 * The first cut said 未冻结 for an unread measurement too, which printed 「未冻结」 in the same row as the
 * `≥ 95.00%` the case had frozen — the card contradicting itself about the only fact it exists to carry.
 */
const FLOOR_VERDICT_ZH: Record<SymbolFloorRow["verdict"], string> = {
  fail: "低于地板",
  pass: "过地板",
  unmeasured: "未测量",
  unset: "未冻结",
};

const BAND_SENTENCE: Record<SymbolBandPlacement, string> = {
  above: "帧到时行情已经走过上限：这一格是追涨，规则点名拒绝。",
  below: "帧到时行情还没走到下限：方向没被价格确认，规则不放行。",
  in: "帧到时行情落在带内：这一条是过的。",
  unmeasured: "这一帧没有可比的帧前收盘价，行情幅度无从测量——缺口是显式的，不是零。",
};

/**
 * 交易视角 · 最近一帧怎么读 — the capital lane's reading of this token's newest frame (#282, artifact v8).
 *
 * The reading card above answers "what happened"; this answers "was there a trade in it", and the two use
 * different thresholds on purpose. Its whole job is to make that gap visible on one screen: a frame can be
 * 利多, pushed, and still refused on every one of the three questions below.
 *
 * Nothing here is a rule the browser holds. The quadrant, the band and the floors all come off the case the
 * frame authored, frozen at the moment it was decided — including the measurement window in the band's own
 * caption, which is five minutes on today's strategy and was an hour when the artifact was drawn.
 */
/**
 * What the panel may say about a batch it has not got. `perspective == null` is only an answer once the
 * read has answered: before that it is a question, and on a failure it is a question that failed.
 */
export type PerspectiveRead = "ready" | "loading" | "failed";

const UNREAD_ZH: Record<Exclude<PerspectiveRead, "ready">, string> = {
  failed: "资本通道的账本这次没读到——这不是「没有案例」，是没读到；下一轮轮询会再问一次。",
  loading: "正在读资本通道的账本…",
};

export function NewsSymbolPerspective({
  perspective,
  read,
}: {
  perspective: SymbolPerspective | null;
  read: PerspectiveRead;
}) {
  return (
    <Card
      flush
      hint="阅读卡的「利多」不是交易信号；交易侧只认象限、带内与地板"
      title="交易视角 · 最近一帧怎么读"
    >
      {perspective == null ? (
        <NewsEmptyNote>
          {/*
           * Three answers, not one. "The lane never opened a case", "we have not asked yet" and "we asked
           * and the read failed" were all rendering as the first — the strongest possible positive claim,
           * made loudest exactly when the page knew least.
           */}
          {read === "ready"
            ? "资本通道没有为这个代币开过案，也没有更早的单还在场——三个问题都没有被问过，不是问了没有答案。"
            : UNREAD_ZH[read]}
        </NewsEmptyNote>
      ) : (
        <div className="news-symbol-perspective">
          <article>
            <small>OI / 价格象限</small>
            <div className="news-symbol-quadrants">
              {perspective.quadrants.map((cell) => (
                <span
                  className="news-symbol-quadrant"
                  data-active={cell.active || undefined}
                  key={cell.key}
                >
                  <b>{cell.label}</b>
                  <code>{cell.code}</code>
                </span>
              ))}
            </div>
            <p>
              {perspective.quadrantNote ?? "只有增仓象限允许开仓——减仓是仓位在离场，没有东西可跟。"}
            </p>
          </article>

          <article>
            <small>{perspective.band?.caption ?? "帧前已走行情"} · 只有带内可开</small>
            {perspective.band ? (
              <>
                <div className="news-symbol-band">
                  <span className="news-symbol-band-track">
                    {perspective.band.segments.map((segment) => (
                      <span
                        data-tone={segment.tone}
                        key={segment.key}
                        style={{ flexGrow: segment.flex }}
                      />
                    ))}
                  </span>
                  {perspective.band.markerPercent == null ? null : (
                    <span
                      className="news-symbol-band-marker"
                      /* Its label is centred on the line, so near either end it would hang off the card;
                         `data-edge` anchors it inward instead of letting it clip. */
                      data-edge={edgeOf(perspective.band.markerPercent)}
                      style={{ left: `${perspective.band.markerPercent}%` }}
                    >
                      <b>{perspective.band.measured}</b>
                    </span>
                  )}
                </div>
                <div className="news-symbol-band-ticks">
                  {/* Ticks sit on the boundary they name, not spread evenly: the band is a non-linear
                      slice of the domain, and evenly spaced labels would put `10%` under the wrong edge. */}
                  {perspective.band.ticks.map((tick) => (
                    <small
                      className="news-symbol-band-tick"
                      data-anchor={tick.anchor}
                      key={tick.label}
                      style={{ left: `${tick.percent}%` }}
                    >
                      {tick.label}
                    </small>
                  ))}
                </div>
                <p>{BAND_SENTENCE[perspective.band.placement]}</p>
              </>
            ) : (
              <p>这个案例没有冻结价格带的两个阈值，无法说它当时被什么带子判过。</p>
            )}
          </article>

          <article>
            {/* The artifact calls this 研究分桶 and names the research buckets by hand. The buckets it
                names are the thresholds of a strategy that no longer runs; what a reader needs is the
                frame's own measurements against the floors *this* case was frozen against, which is the
                same question with an answer that stays true. */}
            <small>地板对照 · 最近一帧过了几条</small>
            <dl className="news-symbol-floors">
              {perspective.floors.map((floor) => (
                /*
                 * Four `dd`s, not a `dd` plus a `span` and a `small`: a `div` inside a `dl` may hold only
                 * `dt`/`dd`, and the verdict and the threshold — the two facts this card exists to carry —
                 * were reaching the accessibility tree as free-floating text bound to no term.
                 */
                <div key={floor.key}>
                  <dt>{floor.label}</dt>
                  <dd className="news-symbol-floor-measured">{floor.measured}</dd>
                  <dd className="news-symbol-floor-verdict" data-verdict={floor.verdict}>
                    {FLOOR_VERDICT_ZH[floor.verdict]}
                  </dd>
                  <dd className="news-symbol-floor-threshold">{floor.floor}</dd>
                </div>
              ))}
            </dl>
            {/* Counted off the rows above, never asserted: see `floorsNote`. */}
            <p>{perspective.floorsNote}</p>
          </article>
        </div>
      )}
      <NewsSourceLine
        note={
          perspective == null
            ? undefined
            : `读的是 ${STRATEGY_ZH[perspective.strategyId] ?? perspective.strategyId} 冻结在案上的阈值，不是今天的配置${perspective.frameAtMs == null ? "" : `；帧于 ${displayTime(perspective.frameAtMs)}`}`
        }
        /*
         * Both endpoints, because the card reads both: the quadrant and the band come off the case, and
         * every figure in 地板对照 comes off the frame. A line naming only the first pointed a reader
         * auditing 鲸鱼盈利 at a response that does not carry it.
         */
        path="GET /api/trading/intents?underlying={base} → regime · pre_move_bps · strategy_config ＋ GET /api/news/feed?symbol={base} → events[].oi"
      />
    </Card>
  );
}
