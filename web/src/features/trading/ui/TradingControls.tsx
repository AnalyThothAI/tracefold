import { ApiError } from "@lib/api/client";
import { ActionButton } from "@shared/ui/ActionButton";
import { AlertDialog } from "radix-ui";
import { useState } from "react";

import { useIssueTradingCommandWithToken } from "../api/tradingQueries";

type CommandAction = "pause" | "resume" | "flatten";
type CommandEnvelope = {
  action: CommandAction;
  requestId: string;
  requestedAtMs: number;
  text: string;
};

export function TradingControls({
  accountFlatProven,
  entriesPaused,
  mode,
  token,
}: {
  accountFlatProven: boolean;
  entriesPaused: boolean;
  mode: "disabled" | "paper" | "live";
  token: string;
}) {
  const command = useIssueTradingCommandWithToken(token);
  const [reason, setReason] = useState("operator console");
  const [pendingAction, setPendingAction] = useState<CommandAction | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [retryEnvelope, setRetryEnvelope] = useState<CommandEnvelope | null>(null);
  const disabled = mode === "disabled" || command.isPending;

  const issue = (action: CommandAction) => {
    const normalizedReason = reason.trim() || "operator console";
    const envelope =
      retryEnvelope?.action === action
        ? retryEnvelope
        : {
            action,
            requestId: crypto.randomUUID(),
            requestedAtMs: Date.now(),
            text:
              action === "pause"
                ? `/pause ${normalizedReason}`
                : action === "resume"
                  ? `/resume ${normalizedReason} CONFIRM`
                  : "/flatten account 30 CONFIRM",
          };
    setRetryEnvelope(envelope);
    command.mutate(
      {
        requestId: envelope.requestId,
        requestedAtMs: envelope.requestedAtMs,
        text: envelope.text,
      },
      {
        onSuccess: () => {
          setRetryEnvelope(null);
          setPendingAction(null);
          setConfirmation("");
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status < 500) setRetryEnvelope(null);
        },
      },
    );
  };

  return (
    <section aria-label="执行控制" className="trading-control-panel">
      <div className="trading-control-copy">
        <b>执行控制</b>
        <span>按钮只写入 Command；页面随后从 Runtime 与 venue 回执更新进度。</span>
      </div>
      <label className="trading-control-reason">
        <span>操作原因</span>
        <input maxLength={256} onChange={(event) => setReason(event.target.value)} value={reason} />
      </label>
      <div className="trading-control-actions">
        <ActionButton
          disabled={disabled || entriesPaused}
          onClick={() => issue("pause")}
          variant="caution"
        >
          Pause entries
        </ActionButton>
        <ActionButton
          disabled={disabled || !entriesPaused}
          onClick={() => setPendingAction("resume")}
          variant="positive"
        >
          Resume / Arm
        </ActionButton>
        <ActionButton
          disabled={disabled || accountFlatProven}
          onClick={() => setPendingAction("flatten")}
          variant="negative"
        >
          Flatten account
        </ActionButton>
      </div>
      {mode === "disabled" ? (
        <p className="trading-control-message" data-tone="caution">
          execution.mode=disabled；控制已锁定，不会写入无 Runtime 可处理的 Command。
        </p>
      ) : null}
      {command.data ? (
        <p aria-live="polite" className="trading-control-message">
          Command 已持久化 · {command.data.command_id.slice(0, 12)}；这不代表 Runtime
          受理、订单或成交。
        </p>
      ) : null}
      {command.isError ? (
        <p aria-live="assertive" className="trading-control-message" data-tone="caution">
          {command.error instanceof ApiError && command.error.status < 500
            ? `Command 未写入 · ${command.error.code ?? command.error.message}`
            : "提交结果未知；请先核对 Command 账本，重试会复用同一 request ID、时钟和文本。"}
        </p>
      ) : null}

      <AlertDialog.Root
        onOpenChange={(open) => {
          if (!open) {
            setPendingAction(null);
            setConfirmation("");
          }
        }}
        open={pendingAction !== null}
      >
        <AlertDialog.Portal>
          <AlertDialog.Overlay className="trading-confirm-overlay" />
          <AlertDialog.Content className="trading-confirm-dialog">
            <AlertDialog.Title>
              {pendingAction === "flatten" ? "确认收敛全部 exposure" : "确认恢复新增 exposure"}
            </AlertDialog.Title>
            <AlertDialog.Description>
              {pendingAction === "flatten"
                ? "Flatten 会暂停开仓并让 Runtime 对其拥有的仓位执行 reduce-only 退出；只有之后的新鲜 Binance 私有对账才能证明账户已平。"
                : "Resume 只解除控制暂停；风险、行情、审计或对账不满足时 entries-armed 仍会保持 false。"}
            </AlertDialog.Description>
            {mode === "live" ? (
              <label className="trading-live-confirmation">
                <span>Live 模式：输入 CONFIRM 进行二次确认</span>
                <input
                  autoComplete="off"
                  onChange={(event) => setConfirmation(event.target.value)}
                  value={confirmation}
                />
              </label>
            ) : null}
            <div className="trading-confirm-actions">
              <AlertDialog.Cancel asChild>
                <ActionButton>取消</ActionButton>
              </AlertDialog.Cancel>
              <AlertDialog.Action asChild>
                <ActionButton
                  disabled={command.isPending || (mode === "live" && confirmation !== "CONFIRM")}
                  onClick={() => pendingAction && issue(pendingAction)}
                  variant={pendingAction === "flatten" ? "negative" : "positive"}
                >
                  {command.isPending ? "正在持久化…" : "确认写入 Command"}
                </ActionButton>
              </AlertDialog.Action>
            </div>
          </AlertDialog.Content>
        </AlertDialog.Portal>
      </AlertDialog.Root>
    </section>
  );
}
