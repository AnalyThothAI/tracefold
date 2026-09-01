import type {
  TradingExecutionObservation,
  TradingOperatorIntent,
  TradingSignal,
} from "../api/tradingQueries";

export type ProgressTone = "complete" | "current" | "pending" | "rejected" | "expired";

export type ExecutionProgressStep = {
  label: string;
  tone: ProgressTone;
};

export type ExecutionProgress = {
  label: string;
  steps: ExecutionProgressStep[];
};

export function commandProgress(
  command: TradingOperatorIntent,
  observations: TradingExecutionObservation[],
): ExecutionProgress {
  const correlated = observations.filter((item) => item.command_id === command.command_id);
  const runtimeAccepted =
    command.disposition === "accepted" ||
    command.disposition === "completed" ||
    correlated.some(
      (item) =>
        item.normalized_kind === "readiness" && item.summary?.control_stage === "runtime_accepted",
    );
  const runtimeRejected = command.disposition === "rejected";
  const orderAccepted = correlated.some(
    (item) =>
      ["order", "protection"].includes(item.normalized_kind) && item.summary?.status === "accepted",
  );
  const fillObserved = correlated.some((item) => item.normalized_kind === "fill");
  const accountFlat =
    command.disposition === "completed" && command.disposition_reason === "binance_account_flat";
  const controlCompleted =
    command.action !== "flatten" && command.disposition === "accepted" && !runtimeRejected;
  const expired = command.expired && !command.disposition;
  const venueApplicable = command.action === "flatten";

  const steps: ExecutionProgressStep[] = [
    { label: "已持久化", tone: "complete" },
    {
      label: runtimeRejected ? "Runtime 拒绝" : "Runtime 受理",
      tone: runtimeRejected
        ? "rejected"
        : runtimeAccepted
          ? "complete"
          : expired
            ? "expired"
            : "current",
    },
    {
      label: venueApplicable
        ? fillObserved
          ? "成交已观察"
          : orderAccepted
            ? "订单已接受"
            : "等待 venue"
        : "无需 venue",
      tone: venueApplicable
        ? fillObserved
          ? "complete"
          : orderAccepted
            ? "current"
            : expired
              ? "expired"
              : "pending"
        : runtimeAccepted
          ? "complete"
          : "pending",
    },
    {
      label: accountFlat
        ? "账户已平 · 私有对账证明"
        : controlCompleted
          ? "控制已完成"
          : expired
            ? "已过期"
            : "等待完成",
      tone:
        accountFlat || controlCompleted
          ? "complete"
          : expired
            ? "expired"
            : runtimeRejected
              ? "rejected"
              : "pending",
    },
  ];
  return {
    label: accountFlat
      ? "ACCOUNT FLAT · PROVEN"
      : controlCompleted
        ? "COMPLETED"
        : runtimeRejected
          ? "RUNTIME REJECTED"
          : fillObserved
            ? "FILL OBSERVED"
            : orderAccepted
              ? "ORDER ACCEPTED"
              : runtimeAccepted
                ? "RUNTIME ACCEPTED"
                : expired
                  ? "EXPIRED"
                  : "PERSISTED",
    steps,
  };
}

export function signalProgress(
  signal: TradingSignal,
  observations: TradingExecutionObservation[],
): ExecutionProgress {
  const correlated = observations.filter((item) => item.signal_id === signal.signal_id);
  const disposition = correlated.find((item) => item.normalized_kind === "signal_disposition");
  const accepted = disposition?.summary?.disposition === "accepted";
  const rejected = Boolean(disposition && !accepted);
  const orderAccepted = correlated.some(
    (item) => item.normalized_kind === "order" && item.summary?.status === "accepted",
  );
  const fillObserved = correlated.some((item) => item.normalized_kind === "fill");
  const positionOpened = correlated.some(
    (item) => item.normalized_kind === "position" && item.summary?.status === "opened",
  );
  const expired = signal.expired && !disposition;
  return {
    label: positionOpened
      ? "POSITION OPENED"
      : fillObserved
        ? "FILL OBSERVED"
        : orderAccepted
          ? "ORDER ACCEPTED"
          : accepted
            ? "RUNTIME ACCEPTED"
            : rejected
              ? "RUNTIME REJECTED"
              : expired
                ? "EXPIRED"
                : "PERSISTED",
    steps: [
      { label: "Signal 已持久化", tone: "complete" },
      {
        label: rejected ? "Runtime 拒绝" : "Runtime 受理",
        tone: rejected ? "rejected" : accepted ? "complete" : expired ? "expired" : "current",
      },
      {
        label: fillObserved ? "成交已观察" : orderAccepted ? "订单已接受" : "等待 venue",
        tone: fillObserved
          ? "complete"
          : orderAccepted
            ? "current"
            : expired
              ? "expired"
              : "pending",
      },
      {
        label: positionOpened ? "仓位已打开" : expired ? "已过期" : "等待仓位事实",
        tone: positionOpened ? "complete" : expired ? "expired" : rejected ? "rejected" : "pending",
      },
    ],
  };
}
