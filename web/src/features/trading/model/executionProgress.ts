import type {
  TradingExecutionObservation,
  TradingOperatorIntent,
  TradingSignal,
} from "../api/tradingQueries";

export type ProgressTone =
  | "complete"
  | "current"
  | "pending"
  | "ambiguous"
  | "rejected"
  | "expired";

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
  const runtimeExpired = command.disposition_reason === "expired";
  const manualEntry = command.action === "manual_entry";
  const runtimeAmbiguous =
    manualEntry &&
    ["unknown_query_first", "replayed_query_first"].includes(command.disposition_reason ?? "");
  const runtimeRejected = command.disposition === "rejected" && !runtimeExpired;
  const orderAccepted = correlated.some(
    (item) =>
      ["order", "protection"].includes(item.normalized_kind) && item.summary?.status === "accepted",
  );
  const fillObserved = correlated.some((item) => item.normalized_kind === "fill");
  const positionOpened = correlated.some(
    (item) => item.normalized_kind === "position" && item.summary?.status === "opened",
  );
  const accountFlat =
    command.disposition === "completed" && command.disposition_reason === "binance_account_flat";
  const controlCompleted =
    !["flatten", "manual_entry"].includes(command.action) &&
    command.disposition === "accepted" &&
    !runtimeRejected;
  const expired = runtimeExpired || (command.expired && !command.disposition);
  const venueApplicable = ["flatten", "manual_entry"].includes(command.action);

  const steps: ExecutionProgressStep[] = [
    { label: "已持久化", tone: "complete" },
    {
      label: runtimeRejected
        ? "Runtime 拒绝"
        : runtimeAmbiguous
          ? "Runtime 结果不确定"
          : expired
            ? "Runtime 已过期"
            : "Runtime 受理",
      tone: runtimeRejected
        ? "rejected"
        : runtimeAmbiguous
          ? "ambiguous"
          : expired
            ? "expired"
            : runtimeAccepted
              ? "complete"
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
              : runtimeAmbiguous
                ? "ambiguous"
                : "pending"
        : runtimeAccepted
          ? "complete"
          : "pending",
    },
    {
      label: accountFlat
        ? "账户已平 · 私有对账证明"
        : manualEntry && positionOpened
          ? "仓位已打开"
          : controlCompleted
            ? "控制已完成"
            : expired
              ? "已过期"
              : manualEntry
                ? "等待仓位事实"
                : "等待完成",
      tone:
        accountFlat || positionOpened || controlCompleted
          ? "complete"
          : expired
            ? "expired"
            : runtimeRejected
              ? "rejected"
              : runtimeAmbiguous
                ? "ambiguous"
                : "pending",
    },
  ];
  return {
    label: accountFlat
      ? "ACCOUNT FLAT · PROVEN"
      : manualEntry && positionOpened
        ? "POSITION OPENED"
        : controlCompleted
          ? "COMPLETED"
          : runtimeRejected
            ? "RUNTIME REJECTED"
            : fillObserved
              ? "FILL OBSERVED"
              : orderAccepted
                ? "ORDER ACCEPTED"
                : runtimeAmbiguous
                  ? "RUNTIME AMBIGUOUS"
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
  const rawDisposition = disposition?.summary?.disposition;
  const dispositionValue = typeof rawDisposition === "string" ? rawDisposition : "";
  const accepted = dispositionValue === "accepted";
  const runtimeExpired = dispositionValue === "expired";
  const ambiguous = ["unknown_query_first", "replayed_query_first"].includes(
    dispositionValue ?? "",
  );
  const rejected = Boolean(disposition && !accepted && !ambiguous && !runtimeExpired);
  const orderAccepted = correlated.some(
    (item) => item.normalized_kind === "order" && item.summary?.status === "accepted",
  );
  const fillObserved = correlated.some((item) => item.normalized_kind === "fill");
  const positionOpened = correlated.some(
    (item) => item.normalized_kind === "position" && item.summary?.status === "opened",
  );
  const expired = runtimeExpired || (signal.expired && !disposition);
  return {
    label: positionOpened
      ? "POSITION OPENED"
      : fillObserved
        ? "FILL OBSERVED"
        : orderAccepted
          ? "ORDER ACCEPTED"
          : accepted
            ? "RUNTIME ACCEPTED"
            : ambiguous
              ? "RUNTIME AMBIGUOUS"
              : rejected
                ? "RUNTIME REJECTED"
                : expired
                  ? "EXPIRED"
                  : "PERSISTED",
    steps: [
      { label: "Signal 已持久化", tone: "complete" },
      {
        label: rejected
          ? "Runtime 拒绝"
          : ambiguous
            ? "Runtime 结果不确定"
            : expired
              ? "Runtime 已过期"
              : "Runtime 受理",
        tone: rejected
          ? "rejected"
          : accepted
            ? "complete"
            : ambiguous
              ? "ambiguous"
              : expired
                ? "expired"
                : "current",
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
        tone: positionOpened
          ? "complete"
          : expired
            ? "expired"
            : rejected
              ? "rejected"
              : ambiguous
                ? "ambiguous"
                : "pending",
      },
    ],
  };
}
