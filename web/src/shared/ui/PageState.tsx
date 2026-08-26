import { cn } from "@lib/utils";
import type { ReactNode } from "react";

import { ActionButton } from "./ActionButton";
import "./PageState.css";

type PageStateLayout = "route" | "panel" | "inline";

/**
 * The states a surface can be in besides "showing data". Every route uses these rather than inventing its
 * own, so a reader recognises a loading feed and a loading status page as the same thing.
 *
 * Loading keeps the shape of what is coming — rows, not a spinner — because the page it replaces is a list
 * and a shimmering list says how much is on its way. Stale keeps the previous answer on screen and marks it
 * busy: a poll-driven console that blanks on every refetch is unreadable.
 *
 * The v7 artifact draws a bone for the page title too (#256). This console does not: the title is static
 * copy it already knows, and shimmering a word that is not waiting on anything is theatre. What shimmers is
 * exactly what the server has yet to answer — the metric band, the rows, and the 2px sweep at the top of
 * the frame that says a request is in flight at all.
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
  const detail = errorDetail(error);
  return (
    <div className="page-state-error" role="alert">
      <b>{detail.title}</b>
      <span>{detail.message}</span>
      {onRetry ? (
        <ActionButton onClick={onRetry} size="sm">
          重试
        </ActionButton>
      ) : null}
    </div>
  );
}

export function Stale({
  children,
  className,
  failedRefresh,
  onRetry,
  updating,
}: {
  children: ReactNode;
  className?: string;
  failedRefresh?: ReactNode;
  onRetry?: () => void;
  updating: boolean;
}) {
  return (
    <div
      aria-busy={updating}
      className={cn("page-state-stale", className, updating && "page-state-stale-updating")}
    >
      {failedRefresh ? (
        <div className="page-state-stale-error" role="alert">
          <span>{failedRefresh}</span>
          {onRetry ? (
            <ActionButton onClick={onRetry} size="sm">
              重试
            </ActionButton>
          ) : null}
        </div>
      ) : null}
      {children}
      {updating ? <span className="sr-only">正在更新</span> : null}
    </div>
  );
}

/**
 * The metric band before its figures land: five tiles on the same 5-column grid and hairline dividers the
 * real `MetricRow` uses, so the page does not change height when the read answers.
 */
export function TileSkeleton({
  className,
  label,
  tiles = 5,
}: {
  className?: string;
  label: string;
  tiles?: number;
}) {
  return (
    <div
      aria-busy="true"
      aria-label={label}
      className={cn("page-state-tile-skeleton", className)}
      role="status"
    >
      {Array.from({ length: tiles }, (_, index) => (
        <div aria-hidden className="page-state-tile" key={index}>
          <span className="page-state-bone page-state-tile-eyebrow" />
          <span className="page-state-bone page-state-tile-value" />
          <span className="page-state-bone page-state-tile-caption" />
        </div>
      ))}
    </div>
  );
}

/**
 * The frame's own "a request is in flight" line (#256): 2px at the very top, an indigo sweep, and nothing
 * else. It is `aria-hidden` because every surface underneath already announces its own busy state — a
 * second live region saying "loading" would make a screen reader read the whole cold start twice.
 */
export function RouteProgress() {
  return <div aria-hidden className="page-state-route-progress" />;
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
      {/* The header band the real list has, so the first row does not jump up when the answer lands. */}
      {compact ? null : <div aria-hidden className="page-state-table-head" />}
      {Array.from({ length: rows }, (_, index) => (
        <div
          aria-hidden
          className="page-state-table-row"
          key={index}
          /* Rows fade with depth, exactly as the artifact draws them: the list reads as receding into what
             has not arrived yet rather than as eight equal grey bars. */
          style={{ opacity: Math.max(0.28, 1 - index * 0.09) }}
        >
          <span className="page-state-bone page-state-table-block-leading" />
          <span className="page-state-table-lines">
            <span className="page-state-bone page-state-table-block-body" />
            <span className="page-state-bone page-state-table-block-sub" />
          </span>
          <span className="page-state-bone page-state-table-block-trailing" />
        </div>
      ))}
    </div>
  );
}

function errorDetail(error: unknown): { message: string; title: string } {
  const status =
    typeof error === "object" && error && "status" in error && typeof error.status === "number"
      ? error.status
      : null;
  if (status === 401) {
    return { message: "当前凭证无效或已过期，请刷新页面后重试。", title: "无权限访问" };
  }
  if (status === 403) {
    return { message: "当前凭证无权访问此数据。", title: "无权限访问" };
  }
  if (error instanceof TypeError && error.message === "Failed to fetch") {
    return { message: "无法连接服务，请检查网络后重试。", title: "请求失败" };
  }
  if (error instanceof globalThis.Error) return { message: error.message, title: "请求失败" };
  if (typeof error === "string") return { message: error, title: "请求失败" };
  return { message: "未知错误", title: "请求失败" };
}
