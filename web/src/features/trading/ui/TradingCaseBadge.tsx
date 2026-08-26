import { useTradingEventCaseWithToken } from "../api/tradingQueries";
import { CASE_STATE_ZH, gateReasonLabel, GATE_STATUS_ZH } from "../model/tradingLabels";

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
 * No case — asked, and the lane never opened one. Since #264 that answer carries *why*: the admission
 * ledger's stage and reason, verbatim beside their Chinese. 未成案 on its own was the same chip for "below
 * the liquidity floor", "no perp at the venue whose OI moved" and "the lane never evaluated it", and the
 * three are different operational facts. A frame with no ledger row at all keeps the bare chip, because
 * "not evaluated under any gate version" is not a refusal and must not be drawn as one.
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
    const reason = data.gate_stage && data.gate_reason ? `${data.gate_stage}:${data.gate_reason}` : "";
    return (
      <span
        className="trading-case-badge"
        data-gate={data.gate_status ?? undefined}
        title={
          reason
            ? `${GATE_STATUS_ZH[data.gate_status ?? ""] ?? data.gate_status} · ${gateReasonLabel(reason)}`
            : "资本通道尚未在任何 gate 版本下评估过这一帧"
        }
      >
        未成案
        {reason ? <code>{reason}</code> : null}
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
