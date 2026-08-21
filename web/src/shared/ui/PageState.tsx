import { cn } from "@lib/utils";
import type { ReactNode } from "react";

import { ActionButton } from "./ActionButton";
import "./PageState.css";

type PageStateLayout = "route" | "panel" | "inline";

/**
 * The four states a surface can be in besides "showing data". Every route uses these rather than inventing
 * its own, so a reader recognises a loading feed and a loading status page as the same thing.
 *
 * Loading keeps the shape of what is coming — rows, not a spinner — because the page it replaces is a list
 * and a shimmering list says how much is on its way. Stale keeps the previous answer on screen and marks it
 * busy: a poll-driven console that blanks on every refetch is unreadable.
 */
export function Loading({
  label,
  layout,
  rows = 5,
}: {
  label: string;
  layout: PageStateLayout;
  rows?: number;
}) {
  return (
    <TableSkeleton
      className={cn("page-state-loading", `page-state-layout-${layout}`)}
      compact={layout === "inline"}
      label={label}
      rows={rows}
    />
  );
}

export function Empty({
  action,
  hint,
  title,
}: {
  action?: ReactNode;
  hint?: ReactNode;
  title: ReactNode;
}) {
  return (
    <div className="page-state-empty">
      <b>{title}</b>
      {hint ? <span>{hint}</span> : null}
      {action ? <div className="page-state-empty-action">{action}</div> : null}
    </div>
  );
}

export function Error({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="page-state-error" role="alert">
      <b>请求失败</b>
      <span>{errorMessage(error)}</span>
      {onRetry ? (
        <ActionButton onClick={onRetry} size="sm">
          重试
        </ActionButton>
      ) : null}
    </div>
  );
}

export function Stale({ children, updating }: { children: ReactNode; updating: boolean }) {
  return (
    <div
      aria-busy={updating}
      className={cn("page-state-stale", updating && "page-state-stale-updating")}
    >
      {children}
      {updating ? <span className="sr-only">正在更新</span> : null}
    </div>
  );
}

export function TableSkeleton({
  className,
  compact = false,
  label = "正在加载",
  rows = 5,
}: {
  className?: string;
  compact?: boolean;
  label?: string;
  rows?: number;
}) {
  return (
    <div
      aria-busy="true"
      aria-label={label}
      className={cn(
        "page-state-table-skeleton",
        compact && "page-state-table-skeleton-compact",
        className,
      )}
      role="status"
    >
      {Array.from({ length: rows }, (_, index) => (
        <div aria-hidden className="page-state-table-row" key={index}>
          <span className="page-state-table-block page-state-table-block-leading" />
          <span className="page-state-table-block page-state-table-block-body" />
          <span className="page-state-table-block page-state-table-block-trailing" />
        </div>
      ))}
    </div>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof globalThis.Error) return error.message;
  if (typeof error === "string") return error;
  return "未知错误";
}
