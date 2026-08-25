import { Card } from "@shared/ui/Card";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";
import { useSearchParams } from "react-router-dom";

import {
  NEWS_OI_TABS,
  useNewsOiFeedWithToken,
  useNewsStatusWithToken,
  type NewsOiTab,
} from "../../api/newsQueries";
import { absoluteTime, formatCount, percent } from "../../model/newsLabels";
import { oiTabCount, oiWindowHours, parseOiTab } from "../../model/oiSignals";
import { NewsPageHeader, NewsPageShell, NewsPageStamp } from "../chrome/NewsChrome";

import { NewsOiFrameTable } from "./NewsOiFrameTable";
import { NewsOiGates } from "./NewsOiGates";
import { NewsOiSource } from "./NewsOiSource";
import { NewsOiWindow } from "./NewsOiWindow";

import "./newsOi.css";

/**
 * 持仓异动监控 — #137's deterministic open-interest lane, which is roughly a fifth of the day's volume and
 * until now had no surface of its own (#207).
 *
 * Two server reads and nothing else: `/api/news/status` for the lane's own section (thresholds, the 24 h
 * counts by gate, the live window) and `/api/news/feed` filtered to `admission=telemetry_deterministic` for
 * the frames. Every number on the page is one of their fields, and each panel says which.
 *
 * What this page deliberately does not do: it draws no open-interest curve (the provider emits a frame only
 * when its own trigger fires, so there are no samples between them and a line through them would be
 * invented), it puts no current quote on a frame (a price that changes every few seconds beside a fixed
 * post-event measurement reads as a market screen), and it does not let anyone change a threshold here —
 * `news.oi` is operator configuration and this page only reports what it currently is.
 */
export function NewsOiPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parseOiTab(searchParams.get("oi"));
  const statusQuery = useNewsStatusWithToken(token);
  const feedQuery = useNewsOiFeedWithToken(token, tab);

  const status = statusQuery.data;
  const pipeline = status?.pipeline;
  const oi = status?.oi;
  const received = pipeline?.telemetry_received_24h;
  const parsed = pipeline?.telemetry_parsed_24h;
  const failed = pipeline?.telemetry_parse_failed_24h;
  const pushed = pipeline?.telemetry_push_24h;
  const byRule = oi?.by_rule_24h ?? {};
  const counts = Object.fromEntries(
    NEWS_OI_TABS.map((value) => [value, oiTabCount(value, byRule, received)]),
  ) as Record<NewsOiTab, number | null>;
  const hours = oiWindowHours(oi?.policy?.window_ms);

  return (
    <NewsPageShell archetype="scan" className="news-oi-shell" label="持仓异动监控">
      <NewsPageHeader subtitle="持仓异动遥测帧：规则判的，不过模型。" title="持仓异动监控">
        {status ? (
          <NewsPageStamp>更新于 {absoluteTime(status.measured_at_ms).slice(11)}</NewsPageStamp>
        ) : null}
      </NewsPageHeader>

      {statusQuery.isLoading && !status ? (
        <PageState.Loading label="正在读取持仓异动遥测" layout="panel" rows={4} />
      ) : null}
      {statusQuery.isError && !status ? (
        <PageState.Error error={statusQuery.error} onRetry={() => void statusQuery.refetch()} />
      ) : null}

      {status ? (
        <PageState.Stale updating={statusQuery.isFetching || feedQuery.isFetching}>
          <div className="news-oi-body">
            <Card
              hint={
                parsed != null && received
                  ? `解析成功率 ${percent(parsed, received)} · 合格 ${percent(pushed ?? 0, received)}`
                  : undefined
              }
              title="LAST 24H · TELEMETRY"
              titleStyle="eyebrow"
            >
              <MetricRow columns={5} label="过去 24 小时的遥测帧">
                <Metric
                  caption="telemetry_received_24h"
                  eyebrow="RECEIVED"
                  value={count(received)}
                />
                <Metric caption="telemetry_parsed_24h" eyebrow="PARSED" value={count(parsed)} />
                {/*
                 * The one qualifying rule's own 24 h count. The caption names the field rather than the
                 * rule key — `opening_move_with_whale_concentration` does not fit a tile at this width, and
                 * a clipped key is worse than the field that holds it, which the source line spells out.
                 */}
                <Metric
                  caption="oi.by_rule_24h"
                  eyebrow="ELIGIBLE"
                  value={count(byRule.opening_move_with_whale_concentration)}
                />
                <Metric
                  caption="telemetry_push_24h"
                  eyebrow="PUSHED"
                  tone="accent"
                  value={count(pushed)}
                />
                <Metric
                  caption="供应商模板变了才会涨"
                  eyebrow="FAILED"
                  tone={failed ? "caution" : "plain"}
                  value={count(failed)}
                />
              </MetricRow>
              <NewsOiSource path="GET /api/news/status → pipeline.telemetry_*_24h · oi.by_rule_24h" />
            </Card>

            <div className="news-oi-columns">
              <NewsOiGates
                byRule={byRule}
                floors={oi?.trade_floors ?? EMPTY_FLOORS}
                policy={oi?.policy ?? null}
              />
              <NewsOiWindow hours={hours} rows={oi?.window_occupancy ?? []} />
            </div>

            {feedQuery.isError && !feedQuery.data ? (
              <PageState.Error error={feedQuery.error} onRetry={() => void feedQuery.refetch()} />
            ) : (
              <NewsOiFrameTable
                counts={counts}
                floors={oi?.trade_floors ?? EMPTY_FLOORS}
                onTabChange={(next) => setSearchParams(nextOiParams(next), { replace: true })}
                rows={feedQuery.data?.events ?? []}
                tab={tab}
              />
            )}
          </div>
        </PageState.Stale>
      ) : null}
    </NewsPageShell>
  );
}

/**
 * A console older than the API it is talking to still has to render. Zeroes here are honest: they are the
 * defaults the schema itself declares, and every threshold they feed is shown beside its own source line.
 */
const EMPTY_FLOORS = {
  enabled: false,
  max_price_move_bps: 0,
  min_oi_value_usd: 0,
  min_price_move_bps: 0,
  min_whale_long_profit_bps: 0,
  mode: "paper",
  pre_move_lookback_ms: 0,
};

function count(value: number | undefined): string {
  return value == null ? "—" : formatCount(value);
}

function nextOiParams(tab: NewsOiTab): URLSearchParams {
  const params = new URLSearchParams();
  if (tab !== "all") params.set("oi", tab);
  return params;
}
