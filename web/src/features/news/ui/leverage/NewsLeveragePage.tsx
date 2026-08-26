import {
  tradingLedgerEntries,
  useTradingOrdersWithToken,
  useTradingStatusWithToken,
} from "@features/trading";
import { newsOiPath, newsSymbolPath, tradingPath } from "@shared/routing/paths";
import { ActionButton } from "@shared/ui/ActionButton";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useNewsOiFeedWithToken, useNewsQuotesWithToken } from "../../api/newsQueries";
import {
  LEVERAGE_TABS,
  leverageCases,
  leverageFunnel,
  leverageListRows,
  leverageTabCount,
  leverageTopReasons,
  leverageFramelessCount,
  parseLeverageTab,
  type LeverageCase,
  type LeverageTab,
} from "../../model/leverageCases";
import { NewsPageHeader, NewsPageShell } from "../chrome/NewsChrome";

import { NewsLeverageCard } from "./NewsLeverageCard";
import { NewsLeverageDetail } from "./NewsLeverageDetail";
import { NewsLeverageFunnel } from "./NewsLeverageFunnel";
import { NewsLeverageGroupCard } from "./NewsLeverageGroupCard";

import "./newsLeverage.css";

/**
 * 杠杆异动 — the capital lane's reading of the deterministic OI frames (#256).
 *
 * Two questions, two pages. `/news/oi` audits the telemetry: did the provider line parse, did it clear the
 * push gates, did it occupy a window slot. This page answers what the capital lane then decided — on which
 * named rule, with what evidence, and what happened to the money. They deliberately use different
 * thresholds, so merging them would let a reader carry a push decision into a trading one.
 *
 * Three bounded reads, all of them already served and all of them shared with pages that were reading them
 * anyway: `/api/news/feed` filtered to the deterministic lane, one `/api/trading/orders` batch, and
 * `/api/trading/status` for the durable admission funnel and the rules the evidence matrix compares
 * against. Current quotes are a fourth, on their own 15 s rhythm, and they never write back into a case:
 * the frozen plane and the live one sit side by side and are labelled as such.
 *
 * The status read is the capital lane's, not News' (#269). News republishes the operator's `trading`
 * settings document, and since #264/#265 that is no longer the rule set that decides an OI frame:
 * admission belongs to the Candidate Gate and every Alpha threshold to a versioned strategy. A page
 * comparing a smart-money case against `min_whale_long_profit_bps` was measuring it with the 95% floor of
 * the strategy beside it, and printing 冲突 on a row that case had passed.
 *
 * What this page deliberately does not do. It binds no keys — the console cut its keyboard layer whole in
 * #82 and `keyboardLayerHardCut.test.ts` keeps it cut, so the artifact's `j/k/f` bindings are not here and
 * the list is reached by Tab like every other control. It invents no narrative: `oi_momentum_v1` is a pure
 * rule and writes no thesis, so the page names the rule instead of paraphrasing a sentence nobody wrote.
 * And it never lists a frame that authored no case — that population is the OI audit's, in full.
 *
 * Two smaller departures from the artifact, both for the same reason. There is no "N 个新案例到达 · 列表已
 * 固定" pin: selection is URL-owned by `event_id`, so a case arriving at the top cannot move what the pane
 * is showing, and a pill that pinned a list nothing was disturbing would be ceremony. And the four figures
 * above are the four tabs rather than the artifact's five: every one of them is a number a reader can click
 * and count, where 数据新鲜 and 资本警报 would be figures with no list behind them.
 */
export function NewsLeveragePage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parseLeverageTab(searchParams.get("lev"));
  const selectedId = searchParams.get("case");

  const feedQuery = useNewsOiFeedWithToken(token, "all");
  /*
   * The same call the OI audit makes, so React Query serves both from one poll. With no `state` filter the
   * server returns every order in its 24 h window *and* the cases that authored none — both halves are
   * needed here, because a `POLICY_REJECTED` case is exactly where the capital floors bite and it has no
   * order to be found through.
   */
  const tradingQuery = useTradingOrdersWithToken(token);
  /*
   * The capital lane's own status, not News' (#269). Two things live only here: the durable 24 h
   * admission funnel — the only description of a day the lane produced no case in — and the rules the
   * lane actually holds, which after #264/#265 are the Candidate Gate's and each strategy's own, not
   * the operator settings document `/api/news/status` republishes.
   */
  const statusQuery = useTradingStatusWithToken(token);

  /*
   * Recomputed on every render rather than memoised. The wall clock is one of the inputs — ages, hold
   * windows and "still deciding" all read it — so a memo would either pin a stale `Date.now()` or list it
   * as a dependency and never hit. The list is a day's cases, not a day's frames; there is nothing here
   * worth freezing a clock for.
   */
  const thresholds = {
    gate: statusQuery.data?.gate,
    strategies: statusQuery.data?.strategies ?? [],
  };
  const cases = leverageCases(
    feedQuery.data?.events ?? [],
    tradingLedgerEntries(tradingQuery.data),
    thresholds,
    Date.now(),
  );
  const frameless = leverageFramelessCount(cases);
  const visible = cases.filter((item) => LEVERAGE_TABS[tab].predicate(item));
  const rows = leverageListRows(visible);
  const funnel = leverageFunnel(statusQuery.data?.counts, cases);
  const reasons = leverageTopReasons(statusQuery.data?.counts);
  // Which collapsed groups the reader opened. Local because it is a disclosure, not a destination: a
  // URL that carried it would make a shared link open someone else's reading of the same list.
  const [expanded, setExpanded] = useState<readonly string[]>([]);
  /*
   * A `?case=` the current tab filters out is still shown. The URL named one case, and substituting a
   * different one silently — which is what falling straight through to `visible[0]` would do — makes a
   * shared link point at the wrong money. The fallback is only for a link whose case has aged out of the
   * window entirely.
   *
   * Matched against the published `event_id` too. The identity moved from `event_id` to `case_id` (#262),
   * so a link shared before that carries the old one; resolving both means such a link opens its own case
   * instead of quietly opening someone else's.
   */
  const selected =
    (selectedId
      ? cases.find((item) => item.id === selectedId || item.eventId === selectedId)
      : undefined) ?? visible[0];

  const quotesQuery = useNewsQuotesWithToken(token, selected ? [selected.base] : []);
  const quote = quotesQuery.data?.quotes?.[0];

  const loading = feedQuery.isLoading || tradingQuery.isPending || statusQuery.isLoading;
  /*
   * A cold status failure counts. Without it `loading` ends, `failed` stays false, and the page renders
   * the evidence matrix against no thresholds at all — printing 未配置地板 / 不适用 for rules the lane is
   * very much applying. A failed read must read as a failed read, not as "there is no floor".
   */
  const failed =
    (feedQuery.isError && !feedQuery.data) ||
    (tradingQuery.isError && !tradingQuery.data) ||
    (statusQuery.isError && !statusQuery.data);

  return (
    <NewsPageShell archetype="scan" className="news-leverage-shell" label="杠杆异动">
      <NewsPageHeader
        subtitle="少而强的案例：方向、依据、失效条件与资本闭环；帧与闸门在 OI 遥测审计"
        title="市场杠杆结构"
      >
        <MetricRow className="news-leverage-stats" columns={4} label="资本通道当前负载">
          <Metric eyebrow="活跃案例" value={count(cases, "live")} />
          <Metric eyebrow="有方向" tone="accent" value={count(cases, "directional")} />
          <Metric eyebrow="不交易" value={count(cases, "no_trade")} />
          <Metric eyebrow="已结束" value={count(cases, "done")} />
        </MetricRow>
      </NewsPageHeader>

      <div className="news-leverage-toolbar">
        <div aria-label="按案例状态筛选" className="news-segmented" role="tablist">
          {(Object.keys(LEVERAGE_TABS) as LeverageTab[]).map((value) => (
            <button
              aria-selected={tab === value}
              className="news-segmented-option"
              data-active={tab === value || undefined}
              key={value}
              onClick={() => setSearchParams(nextParams(value, null), { replace: true })}
              role="tab"
              type="button"
            >
              {LEVERAGE_TABS[value].label}
              <span className="news-segmented-count">{count(cases, value)}</span>
            </button>
          ))}
        </div>
        {/* The order is a rule, not a score. Saying so is the point: a composite would be unexplainable. */}
        <small className="news-leverage-order">排序：资本风险 → 有方向 → 最近触发；无综合分</small>
      </div>

      {/*
       * The lane's own 24 h funnel, and it stays on the page whether or not anything came of it (#269).
       * On a normal day every tab above reads zero — about 110 frames and one case — and a page that
       * answered that with a blank panel taught its reader that the console was broken.
       */}
      <NewsLeverageFunnel
        reasons={reasons}
        steps={funnel}
        unavailable={statusQuery.isError && !statusQuery.data}
      />

      {loading && !cases.length ? (
        <PageState.Loading label="正在读取杠杆案例" layout="panel" rows={4} />
      ) : null}
      {failed ? (
        <PageState.Error
          error={feedQuery.error ?? tradingQuery.error ?? statusQuery.error}
          onRetry={() => {
            void feedQuery.refetch();
            void tradingQuery.refetch();
            void statusQuery.refetch();
          }}
        />
      ) : null}
      {!loading && !failed && !visible.length ? (
        <PageState.Empty
          action={
            tab === "live" ? null : (
              <ActionButton
                onClick={() => setSearchParams(nextParams("live", null), { replace: true })}
              >
                回到正在发生
              </ActionButton>
            )
          }
          hint={
            cases.length
              ? "换一个标签看看：这一格没有案例，不代表通道没有在跑。"
              : "上面的漏斗是同一段 24 小时：闸门看到了多少帧、卡在哪条规则。0 成案是当前规则的正常输出。"
          }
          title={cases.length ? `${LEVERAGE_TABS[tab].label}里没有案例` : "24 小时内没有成案"}
        />
      ) : null}

      {!failed && visible.length ? (
        <PageState.Stale
          className="news-leverage-body"
          updating={feedQuery.isFetching || tradingQuery.isFetching}
        >
          <section aria-label="案例列表" className="news-leverage-list">
            {rows.map((row) =>
              row.kind === "case" ? (
                <NewsLeverageCard
                  item={row.item}
                  key={row.item.id}
                  onSelect={() => setSearchParams(nextParams(tab, row.item.id), { replace: true })}
                  selected={row.item.id === selected?.id}
                />
              ) : (
                <NewsLeverageGroupCard
                  expanded={expanded.includes(row.key)}
                  key={row.key}
                  onSelect={(item) => setSearchParams(nextParams(tab, item.id), { replace: true })}
                  onToggle={() =>
                    setExpanded((keys) =>
                      keys.includes(row.key)
                        ? keys.filter((value) => value !== row.key)
                        : [...keys, row.key],
                    )
                  }
                  row={row}
                  selectedId={selected?.id}
                />
              ),
            )}
          </section>
          {selected ? (
            <NewsLeverageDetail
              item={selected}
              quote={quote?.requested_symbol === selected.base ? quote : undefined}
              symbolHref={newsSymbolPath(selected.base)}
            />
          ) : null}
        </PageState.Stale>
      ) : null}

      <p className="news-leverage-source">
        {/*
         * The frames arrive one bounded page at a time and the ledger as its own batch, so the join is
         * lossy in one direction. Saying how many by name beats a page that quietly reports a busy day as
         * a quiet one.
         */}
        {frameless > 0 || tradingQuery.data?.complete === false ? (
          <b>
            {frameless > 0
              ? `其中 ${frameless} 条案例没有配套的遥测帧——或是非 OI 触发，或是原帧不在本页（帧按页取）；这些行没有原始线与 OI 测量。`
              : ""}
            {tradingQuery.data?.complete === false
              ? "账本批次已截断，可能还有本页未列出的案例。"
              : ""}
            完整的帧在 <Link to={newsOiPath()}>OI 遥测审计</Link>，完整的账本在{" "}
            <Link to={tradingPath()}>交易 · 模拟仓</Link>。
          </b>
        ) : (
          <>
            帧与闸门在 <Link to={newsOiPath()}>OI 遥测审计</Link>；账本与预算在{" "}
            <Link to={tradingPath()}>交易 · 模拟仓</Link>。
          </>
        )}
      </p>
    </NewsPageShell>
  );
}

function count(cases: readonly LeverageCase[], tab: LeverageTab): string {
  return String(leverageTabCount(cases, tab));
}

function nextParams(tab: LeverageTab, caseId: string | null): URLSearchParams {
  const params = new URLSearchParams();
  if (tab !== "live") params.set("lev", tab);
  if (caseId) params.set("case", caseId);
  return params;
}
