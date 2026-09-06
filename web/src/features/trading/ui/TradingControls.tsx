import { ApiError } from "@lib/api/client";
import { ActionButton } from "@shared/ui/ActionButton";
import { Card } from "@shared/ui/Card";
import { SourceLine } from "@shared/ui/SourceLine";
import { useState } from "react";

import type { TradingExecutionCommand } from "../api/tradingQueries";
import { useIssueTradingCommandWithToken } from "../api/tradingQueries";
import { COMMAND_ACTION_ZH, COMMAND_STAGE_ZH, nsClock } from "../model/tradingLabels";

import { TradingLedgerNote } from "./TradingChrome";

type CommandAction = "pause" | "resume" | "flatten";
type CommandEnvelope = {
  action: CommandAction;
  requestId: string;
  requestedAtMs: number;
  text: string;
};

/**
 * ACT: the three writes, and every Command in the window with the stage the server derived.
 *
 * No confirmation dialog. Pause and Resume only move the control flag, Flatten only asks the Runtime to
 * reduce-only out of what it owns, and none of the three can submit an entry — a modal in front of them
 * taught readers that clicking through it was the dangerous act, when the dangerous act is capital already
 * on the venue. The stage words come from `control_disposition` alone (`tracefold/trading/stages.py`):
 * a flatten reads `recorded → completed`, and `completed` is the private reconciliation proving the slot
 * flat, never an order the browser correlated.
 */
export function TradingControls({
  commands,
  commandsFailed,
  commandsPending,
  entriesPaused,
  mode,
  token,
}: {
  commands: readonly TradingExecutionCommand[];
  commandsFailed: boolean;
  commandsPending: boolean;
  entriesPaused: boolean;
  mode: "disabled" | "paper" | "live";
  token: string;
}) {
  const command = useIssueTradingCommandWithToken(token);
  const [reason, setReason] = useState("operator console");
  const [retryEnvelope, setRetryEnvelope] = useState<CommandEnvelope | null>(null);
  const disabled = mode === "disabled" || !token || command.isPending;

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
                  ? `/resume ${normalizedReason}`
                  : "/flatten account 30",
          };
    setRetryEnvelope(envelope);
    command.mutate(
      {
        requestId: envelope.requestId,
        requestedAtMs: envelope.requestedAtMs,
        text: envelope.text,
      },
      {
        onSuccess: () => setRetryEnvelope(null),
        onError: (error) => {
          if (error instanceof ApiError && error.status < 500) setRetryEnvelope(null);
        },
      },
    );
  };

  return (
    <Card
      flush
      hint="按钮只写入 Command；进度是 Runtime 自己的 control_disposition"
      title="执行控制"
    >
      <section aria-label="执行控制" className="trading-control-panel">
        <label className="trading-control-reason">
          <span>操作原因</span>
          <input
            maxLength={256}
            onChange={(event) => setReason(event.target.value)}
            value={reason}
          />
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
            onClick={() => issue("resume")}
            variant="positive"
          >
            Resume / Arm
          </ActionButton>
          <ActionButton disabled={disabled} onClick={() => issue("flatten")} variant="negative">
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
              : "提交结果未知；请先核对下方 Command 账本，重试会复用同一 request ID、时钟和文本。"}
          </p>
        ) : null}
      </section>

      {/*
       * Action, stage and clock. The reason column repeated the text the operator had just typed into
       * the field above it, and `operator_identity` was the constant `operator-console` on every row a
       * browser wrote — neither is published any more (#537 PR-5).
       */}
      {commands.length ? (
        <div className="trading-command-list">
          {commands.map((item) => (
            <article className="trading-command-row" key={item.command_id}>
              <b>{COMMAND_ACTION_ZH[item.action] ?? item.action}</b>
              <span className="trading-stage" data-stage={item.stage}>
                {COMMAND_STAGE_ZH[item.stage] ?? item.stage}
              </span>
              <span>{nsClock(item.requested_at_ns)}</span>
            </article>
          ))}
        </div>
      ) : (
        <TradingLedgerNote failed={commandsFailed} pending={commandsPending} subject="Command" />
      )}
      <SourceLine path="POST /api/trading/execution/commands · GET /api/trading/executions → commands[].stage" />
    </Card>
  );
}
