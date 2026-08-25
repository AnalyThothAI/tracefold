import { Card, CardNote } from "@shared/ui/Card";

import type { NewsOiWindowSymbol } from "../../api/newsQueries";
import { NewsEmptyNote } from "../chrome/NewsChrome";
import { NewsSourceLine } from "../chrome/NewsSourceLine";

/**
 * Which symbols have already spent their rank slots inside the live window.
 *
 * This is the one thing on the page that is about the *next* frame rather than the last ones: a symbol shown
 * full will have its next qualifying frame withheld by `beyond_window_rank`, and nothing else in the console
 * says so before it happens.
 *
 * The counts are the server's, measured with the same eligibility predicate the judge ranks under. The
 * browser must not fold them out of the frame table: that table is one page of a 24 h window, and a symbol
 * whose earlier frames fell off the page would read as having slots it does not have.
 */
export function NewsOiWindow({
  rows,
  windowLabel,
}: {
  rows: readonly NewsOiWindowSymbol[];
  windowLabel: string;
}) {
  return (
    <Card flush hint={windowLabel ? `过去 ${windowLabel}` : undefined} title="窗口占用">
      {rows.length === 0 ? (
        <NewsEmptyNote>窗口内还没有合格帧，下一帧的名次是第 1 次。</NewsEmptyNote>
      ) : (
        <div className="news-oi-window-rows">
          {rows.map((row) => (
            <article
              className="news-oi-window-row"
              data-full={row.full || undefined}
              key={row.symbol}
            >
              <b className="news-oi-window-symbol">{row.symbol}</b>
              <small className="news-oi-window-note">
                {row.full ? "已满，后续帧会被拦" : "还有名次"}
              </small>
              <span aria-hidden className="news-oi-window-slots">
                {Array.from({ length: Math.max(row.max_rank_in_window, row.used) }, (_, index) => (
                  <span data-spent={index < row.used || undefined} key={index} />
                ))}
              </span>
              <span className="news-oi-window-count">
                {row.used} / {row.max_rank_in_window}
              </span>
            </article>
          ))}
        </div>
      )}
      <CardNote>
        满格的标的，接下来窗口内再来的合格帧会被 <code>beyond_window_rank</code> 拦下。
      </CardNote>
      <NewsSourceLine path="GET /api/news/status → oi.window_occupancy" />
    </Card>
  );
}
