import { useTradingEventCaseWithToken } from "../api/tradingQueries";
import { CASE_STATE_ZH } from "../model/tradingLabels";

import "./tradingCaseBadge.css";

/**
 * Did the capital lane take this Event, and where did it stop? (#207 PR-W4)
 *
 * Three outcomes, and keeping them apart is the whole job.
 *
 * `joinable: false` — this cannot be asked. Only the deterministic OI lane's source key is reconstructible
 * from an `event_id` (`oi:{event_id}:{metric_version}`); the model lane's is a content hash of an artifact
 * and a fingerprint (#154), which no Event id rebuilds. The badge renders nothing at all, because a 未成案
 * chip here would tell a reader the lane declined an Event it never saw.
 *
 * No case — asked, and the lane never opened one. That is a real answer and it says so.
 *
 * A case — the state and, when it stopped, the rule key verbatim. `policy_reason` has no Chinese synonym; it
 * is the string an operator greps for.
 */
export function TradingCaseBadge({
  eventId,
  lane,
  token,
}: {
  eventId: string;
  lane: "oi" | "news";
  token: string;
}) {
  const query = useTradingEventCaseWithToken(token, eventId, lane);
  if (lane !== "oi") return null;
  const data = query.data;
  if (!data || !data.joinable) return null;
  if (!data.case) {
    return (
      <span className="trading-case-badge" title="资本通道读过这一帧，没有为它开案">
        未成案
      </span>
    );
  }
  const caseRow = data.case;
  return (
    <span className="trading-case-badge" data-state={caseRow.state}>
      {CASE_STATE_ZH[caseRow.state] ?? caseRow.state}
      {caseRow.policy_reason ? <code>{caseRow.policy_reason}</code> : null}
      {data.order_state ? <code>{data.order_state}</code> : null}
    </span>
  );
}
