import type { NewsEventKind } from "../../api/newsQueries";
import { eventKindLabel } from "../../model/newsLabels";

import "./newsKindBadge.css";

export function NewsKindBadge({ kind }: { kind: NewsEventKind }) {
  return (
    <span className="news-kind" data-kind={kind}>
      {eventKindLabel(kind)}
    </span>
  );
}
