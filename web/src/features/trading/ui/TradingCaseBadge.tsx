import { tradingCasePath } from "@shared/routing/paths";
import { Link } from "react-router-dom";

import { useTradingGateSourceWithToken } from "../api/tradingQueries";
import { GATE_STATUS_ZH, gateReasonLabel } from "../model/tradingLabels";

import "./tradingCaseBadge.css";

/**
 * What admission decided about this Event's Source, and a link to the Case if it authored one (#331).
 *
 * Three outcomes, and keeping them apart is the whole job.
 *
 * `joinable: false` — this cannot be asked. Only the deterministic OI lane's source key is
 * reconstructible from an `event_id`; the badge renders nothing at all, because a 未成案 chip here would
 * tell a reader the lane declined an Event it never saw.
 *
 * No admission row — asked, and the lane never evaluated it under any gate version. That is not a
 * refusal and is not drawn as one.
 *
 * An admission row — the status and the `stage:reason` verbatim beside its Chinese, and a Case link when
 * the frame authored one. The badge deliberately does not render the Case's decision or the Intent's
 * execution state: those belong to other aggregates, and a chip that carried all three taught readers
 * that a gate refusal and a policy refusal were the same kind of fact.
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
  const query = useTradingGateSourceWithToken(token, eventId, lane);
  if (lane !== "oi") return null;
  const data = query.data;
  if (!data || !data.joinable) return null;
  const decision = data.decision;
  if (!decision) {
    return (
      <span className="trading-case-badge" title="Signal 通道尚未在任何 gate 版本下评估过这一帧">
        未评估
      </span>
    );
  }
  const key =
    decision.gate_stage && decision.gate_reason
      ? `${decision.gate_stage}:${decision.gate_reason}`
      : "";
  const status = decision.gate_status ?? "";
  const label = GATE_STATUS_ZH[status] ?? status;
  return (
    <span
      className="trading-case-badge"
      data-gate={status || undefined}
      title={key ? `${label} · ${gateReasonLabel(key)}` : label}
    >
      {label}
      {key ? <code>{key}</code> : null}
      {decision.case_id ? <Link to={tradingCasePath(decision.case_id)}>案例</Link> : null}
    </span>
  );
}
