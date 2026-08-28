import * as PageState from "@shared/ui/PageState";
import type { ReactNode } from "react";

import { absoluteTime } from "../../model/newsLabels";

import "./newsQuote.css";

type QuoteReadQuery = {
  data: unknown;
  dataUpdatedAt: number;
  error: unknown;
  isError: boolean;
  isFetching: boolean;
  isLoading: boolean;
  refetch: () => unknown;
};

/** One read-state contract for every quote surface; server quote state remains untouched. */
export function NewsQuoteReadState({
  children,
  query,
}: {
  children: ReactNode;
  query: QuoteReadQuery;
}) {
  const hasData = query.data != null;
  const failed = query.isError;

  return (
    <PageState.Stale
      className={failed && hasData ? "news-quote-read-failed" : undefined}
      failedRefresh={
        failed && hasData
          ? `行情读取失败 · 上次成功于 ${query.dataUpdatedAt ? absoluteTime(query.dataUpdatedAt) : "未知"}`
          : undefined
      }
      onRetry={() => void query.refetch()}
      updating={query.isFetching && !failed}
    >
      {query.isLoading && !hasData ? (
        <PageState.Loading label="正在读取行情" layout="panel" rows={1} />
      ) : null}
      {failed && !hasData ? (
        <PageState.Error error={quoteReadError(query.error)} onRetry={() => void query.refetch()} />
      ) : null}
      {children}
    </PageState.Stale>
  );
}

function quoteReadError(error: unknown) {
  const detail = error instanceof Error ? error.message : String(error || "未知错误");
  return new Error(`行情读取失败 · ${detail}`);
}
