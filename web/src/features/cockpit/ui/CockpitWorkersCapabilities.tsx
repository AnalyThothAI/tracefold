import type { OpenApiStatusData } from "@lib/types";

import "./cockpitWorkersCapabilities.css";

type WorkersCapabilities = OpenApiStatusData["runtime"]["workers_runtime"]["capabilities"];
type CapabilityState = WorkersCapabilities[string]["state"];

/**
 * What each Workers capability is doing, apart from whether the process is alive (#553 PR-3).
 *
 * A faulted Trading lane, an unbuildable push sender and an unassemblable News Program no longer stop
 * the process, so `workers_runtime.state` stays `running` beside a dead capability and this list is
 * the only place a reader learns which. It is deliberately a list and not a dashboard: it gates
 * nothing, computes nothing, and prints the three fields the server decided.
 *
 * Ordered by how much it costs the reader: what stopped first, what could not be built, what an
 * operator switched off, what is fine. Within a group the server's key order is kept.
 */
const STATE_ORDER: CapabilityState[] = ["faulted", "unavailable", "disabled", "running"];

const STATE_LABELS: Record<CapabilityState, string> = {
  disabled: "已停用",
  faulted: "已故障",
  running: "运行中",
  unavailable: "不可用",
};

/** `alert` and `caution` are the pipeline tones the rest of the console already uses for these two. */
const STATE_TONES: Record<CapabilityState, string> = {
  disabled: "neutral",
  faulted: "alert",
  running: "done",
  unavailable: "caution",
};

const CAPABILITY_LABELS: Record<string, string> = {
  news_delivery: "推送发送",
  news_editorial: "模型评审",
  news_ingestion: "接收与入库",
  news_instruments: "标的表快照",
  news_quotes: "行情快照",
  news_reactions: "事件回看",
  trading_signal_lane: "交易信号 lane",
};

export function CockpitWorkersCapabilities({
  capabilities,
}: {
  capabilities?: WorkersCapabilities;
}) {
  const rows = Object.entries(capabilities ?? {}).sort(
    ([, left], [, right]) => STATE_ORDER.indexOf(left.state) - STATE_ORDER.indexOf(right.state),
  );
  if (!rows.length) {
    return <em className="cockpit-capabilities-empty">Workers 尚未上报能力状态</em>;
  }
  return (
    <dl aria-label="Workers 能力状态" className="cockpit-capabilities">
      {rows.map(([name, entry]) => (
        <div
          className="cockpit-capability"
          data-state={entry.state}
          data-tone={STATE_TONES[entry.state]}
          key={name}
        >
          <dt>{CAPABILITY_LABELS[name] ?? name}</dt>
          <dd>
            <b>{STATE_LABELS[entry.state] ?? entry.state}</b>
            {entry.reason ? <code>{entry.reason}</code> : null}
          </dd>
        </div>
      ))}
    </dl>
  );
}
