import { Card } from "@shared/ui/Card";
import { Link } from "react-router-dom";

import type { NewsOiPolicy, NewsOiWindowSymbol } from "../../api/newsQueries";
import { oiWindowLabel } from "../../model/oiSignals";
import { NewsSourceLine } from "../chrome/NewsSourceLine";

/**
 * What it means that this name has, or has not, rank left in the live OI window.
 *
 * The one forward-looking fact on the page: a symbol shown full will have its next qualifying frame withheld
 * by `beyond_window_rank`, and this is where a reader who just clicked through from a frame finds that out.
 *
 * The figure itself is the identity band's third tile — the artifact draws it once, there. What is left here
 * is the consequence, which no tile can hold: 2 / 2 is not self-evidently a wall.
 *
 * The count is the server's, measured with the eligibility predicate the judge ranks under. It is not
 * derived from the Events below — that table is a 24 h window and the rank window is four hours, so folding
 * it here would report a fuller window than the judge sees.
 */
export function NewsSymbolWindow({
  occupancy,
  oiPath,
  policy,
}: {
  occupancy: NewsOiWindowSymbol | undefined;
  oiPath: string;
  policy: NewsOiPolicy | null;
}) {
  const windowLabel = oiWindowLabel(policy?.window_ms);
  return (
    <Card
      flush
      hint={windowLabel ? `过去 ${windowLabel}` : undefined}
      title="OI 窗口名次"
      titleStyle="eyebrow"
    >
      <p className="news-symbol-window">
        {occupancy ? (
          <span data-full={occupancy.full || undefined}>
            {occupancy.full
              ? "已满：窗口内后续合格帧会被 beyond_window_rank 拦下。"
              : "还有名次：窗口内下一条合格帧仍会推送。"}
          </span>
        ) : (
          <span className="news-symbol-muted">
            这个标的在当前窗口里没有合格帧——不是被拦，是没有。
          </span>
        )}
        <Link to={oiPath}>OI 遥测审计 →</Link>
      </p>
      <NewsSourceLine path="GET /api/news/status → oi.window_occupancy · oi.policy" />
    </Card>
  );
}
