import {
  CASE_TABS,
  caseChecks,
  caseFigures,
  caseReasonRows,
  caseStateLabel,
  caseTabCount,
  caseVerdict,
  casesForTab,
  defaultCaseTab,
  parseCaseTab,
  policyLabel,
  policyReasonLabel,
  bpsPercent,
  caseClock,
  useTradingCasesWithToken,
  type CaseTab,
  type TradingCase,
} from "@features/trading";
import { newsOiPath, tradingPath } from "@shared/routing/paths";
import { Card } from "@shared/ui/Card";
import * as PageState from "@shared/ui/PageState";
import { Link, useSearchParams } from "react-router-dom";

import { NewsPageHeader, NewsPageShell } from "../chrome/NewsChrome";

import "./newsLeverage.css";

/**
 * 资本判定 — every frozen Case and the frozen evidence it was decided on (#331).
 *
 * One read. `/api/trading/cases` is the Case/Decision aggregate, and this page renders it: the Case's own
 * `policy_config` and `policy_checks` are what a threshold argument is settled from, not the running
 * configuration.
 *
 * **What it no longer does.** The page it replaces re-implemented the capital lane in the browser: it
 * joined the News feed to an Intent batch, inferred a phase from an execution state, re-ran threshold
 * comparisons against `/api/trading/status` and printed 冲突 on rows the Case had passed — because it was
 * measuring a Case frozen last week with a floor edited yesterday. Frames and admission answers belong to
 * the OI audit; execution belongs to 执行与持仓; both are linked rather than restated.
 *
 * The four states are explicit. A cold failure is an error with a retry, a failed refresh keeps the last
 * answer, and an empty window is a *truthful* empty that names the durable totals beside it — `0 成案` is
 * a legitimate output of the current rules and must never be drawn as "the system has no data".
 */
export function NewsLeveragePage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const casesQuery = useTradingCasesWithToken(token);
  const data = casesQuery.data;
  const cases = data?.cases ?? [];
  /*
   * The URL always wins; the fallback is the first tab that has rows. A fixed default of 已形成意图 on a
   * lane that legitimately emits nothing greets every reader with an empty page.
   */
  const tab = defaultCaseTab(cases, parseCaseTab(searchParams.get("lev")));
  const visible = casesForTab(cases, tab);
  const selectedId = searchParams.get("case");
  const selected = (selectedId ? cases.find((item) => item.case_id === selectedId) : undefined) ?? visible[0];
  const figures = caseFigures(data);
  const reasons = caseReasonRows(data);
  const coldFailure = casesQuery.isError && !data;

  return (
    <NewsPageShell archetype="scan" className="news-leverage-shell" label="资本判定">
      <NewsPageHeader
        subtitle="每个案例的终局答案与它被判定时冻结的证据；帧与准入在 OI 来源审计，执行在执行与持仓"
        title="资本判定"
      >
        <dl aria-label="资本通道 24 小时判定" className="news-leverage-figures">
          {figures.map((figure) => (
            <div className="news-leverage-figure" key={figure.key}>
              <dt>{figure.label}</dt>
              <dd data-tone={figure.tone === "plain" ? undefined : figure.tone}>{figure.value}</dd>
            </div>
          ))}
        </dl>
      </NewsPageHeader>

      <div className="news-leverage-toolbar">
        <div aria-label="按案例终局筛选" className="news-segmented" role="tablist">
          {(Object.keys(CASE_TABS) as CaseTab[]).map((value) => (
            <button
              aria-selected={tab === value}
              className="news-segmented-option"
              data-active={tab === value || undefined}
              key={value}
              onClick={() => setSearchParams(nextParams(value, null), { replace: true })}
              role="tab"
              type="button"
            >
              {CASE_TABS[value].label}
              <span className="news-segmented-count">{caseTabCount(cases, value)}</span>
            </button>
          ))}
        </div>
        <small className="news-leverage-order">排序：最近冻结在前；无综合分</small>
      </div>

      {casesQuery.isLoading && !data ? (
        <div className="news-leverage-body">
          <PageState.Loading label="正在读取资本案例" layout="panel" rows={3} />
          <PageState.Loading label="正在读取案例证据" layout="panel" rows={7} />
        </div>
      ) : null}
      {coldFailure ? (
        <PageState.Error error={casesQuery.error} onRetry={() => void casesQuery.refetch()} />
      ) : null}

      {data && !visible.length ? (
        <PageState.Empty
          hint={
            <>
              过去 {data.window_hours} 小时资本通道成案 {figures[0].value} 个。0 成案是当前规则的正常输出；
              上游看见了多少来源、卡在哪条规则，在 <Link to={newsOiPath()}>OI 来源与准入审计</Link>。
            </>
          }
          title={cases.length ? `${CASE_TABS[tab].label}里没有案例` : "24 小时内没有成案"}
        />
      ) : null}

      {data && visible.length ? (
        <PageState.Stale className="news-leverage-body" updating={casesQuery.isFetching}>
          <section aria-label="案例列表" className="news-leverage-list">
            {visible.map((item) => (
              <button
                className="news-leverage-row"
                data-selected={item.case_id === selected?.case_id || undefined}
                key={item.case_id}
                onClick={() => setSearchParams(nextParams(tab, item.case_id), { replace: true })}
                type="button"
              >
                <b>{item.base_symbol}</b>
                <time dateTime={new Date(item.observed_at_ms).toISOString()}>
                  {caseClock(item.observed_at_ms)}
                </time>
                <span>{caseStateLabel(item)}</span>
                <span>{item.intent_id ? "已交接 Intent" : "无 Intent"}</span>
                <code>{item.policy_reason ?? "判定中"}</code>
              </button>
            ))}
          </section>
          {selected ? <CaseDetail item={selected} /> : null}
        </PageState.Stale>
      ) : null}

      {data || casesQuery.isError ? (
        <Card
          flush
          hint="24 小时内每条终局规则命中了多少次；来自 durable 行的有界聚合"
          title="判定规则分布"
        >
          {casesQuery.isError && !data ? (
            <p className="news-leverage-reasons">读取失败，无法陈述分布。</p>
          ) : reasons.length ? (
            <dl className="news-leverage-reasons">
              {reasons.map(([reason, count]) => (
                <div className="news-leverage-reason" key={reason}>
                  <dt>
                    {policyReasonLabel(reason)} <code>{reason}</code>
                  </dt>
                  <dd>{count}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="news-leverage-reasons">过去 24 小时没有终局规则命中。</p>
          )}
        </Card>
      ) : null}

      <p className="news-leverage-source">
        读自 <code>GET /api/trading/cases</code>；帧与准入在{" "}
        <Link to={newsOiPath()}>OI 来源与准入审计</Link>，执行与持仓在{" "}
        <Link to={tradingPath()}>执行与持仓</Link>。
      </p>
    </NewsPageShell>
  );
}

/**
 * One Case in full: its terminal answer, and every condition the policy executed to reach it.
 *
 * Every threshold on screen is the Case's own. That is the whole point of the panel — a console holding
 * only today's configuration cannot explain a Case frozen a week ago, and the version that tried printed
 * conflicts on rows that had passed.
 */
function CaseDetail({ item }: { item: TradingCase }) {
  const checks = caseChecks(item);
  return (
    <section aria-label={`案例 ${item.base_symbol}`} className="news-leverage-detail">
      <Card
        flush
        hint={`${policyLabel(item.policy_id)} · ${item.policy_config_digest.slice(0, 12)}`}
        title={`${item.base_symbol} · ${caseVerdict(item)}`}
      >
        <dl className="news-leverage-reasons">
          <div className="news-leverage-reason">
            <dt>案例</dt>
            <dd>
              <code>{item.case_id}</code>
            </dd>
          </div>
          <div className="news-leverage-reason">
            <dt>触发来源</dt>
            <dd>
              <code>{item.event_id ?? "—"}</code>
            </dd>
          </div>
          <div className="news-leverage-reason">
            <dt>冻结时刻</dt>
            <dd>{caseClock(item.observed_at_ms)}</dd>
          </div>
          <div className="news-leverage-reason">
            <dt>判定时刻</dt>
            <dd>{caseClock(item.decided_at_ms)}</dd>
          </div>
          <div className="news-leverage-reason">
            <dt>合约</dt>
            <dd>{item.provider_symbol ?? "—"}</dd>
          </div>
          <div className="news-leverage-reason">
            <dt>冻结标记价</dt>
            <dd>{item.mark_price ?? "—"}</dd>
          </div>
          <div className="news-leverage-reason">
            <dt>前置涨跌</dt>
            <dd>{bpsPercent(item.pre_move_bps)}</dd>
          </div>
          <div className="news-leverage-reason">
            <dt>Intent</dt>
            <dd>
              {item.intent_id ? (
                <Link to={tradingPath()}>
                  <code>{item.intent_id.slice(0, 16)}</code>
                </Link>
              ) : (
                "未形成"
              )}
            </dd>
          </div>
        </dl>
      </Card>

      <Card
        flush
        hint="这张表的阈值来自案例本身，不是当前配置"
        title={`冻结判定证据 · ${item.manifest_version ?? "—"}`}
      >
        {checks.length ? (
          <div className="news-leverage-checks">
            <div aria-hidden className="news-leverage-check">
              <span>条件</span>
              <span>比较</span>
              <span>阈值</span>
              <span>实测</span>
              <span>结果</span>
            </div>
            {checks.map((check, index) => (
              <div
                className="news-leverage-check"
                data-passed={check.passed}
                key={`${check.check}-${index}`}
              >
                <span>{check.check}</span>
                <span>{check.operator}</span>
                <span>{check.threshold_label}</span>
                <span>{check.measured_label}</span>
                <span>{check.passed ? "通过" : "未通过"}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="news-leverage-reasons">
            这个案例在冻结逐条证据之前写入（#331 之前）。它的终局与规则仍然是{" "}
            <code>{item.policy_reason ?? "—"}</code>，冻结配置在下方。
          </p>
        )}
      </Card>

      <Card flush hint="案例冻结时执行的整组数字" title="冻结策略配置">
        <dl className="news-leverage-reasons">
          {Object.entries(item.policy_config ?? {}).map(([key, value]) => (
            <div className="news-leverage-reason" key={key}>
              <dt>
                <code>{key}</code>
              </dt>
              <dd>{value}</dd>
            </div>
          ))}
          {Object.keys(item.policy_config ?? {}).length === 0 ? (
            <div className="news-leverage-reason">
              <dt>冻结配置</dt>
              <dd>该案例未记录（#331 之前的清单版本）</dd>
            </div>
          ) : null}
        </dl>
      </Card>
    </section>
  );
}

function nextParams(tab: CaseTab, caseId: string | null): URLSearchParams {
  const params = new URLSearchParams();
  if (tab !== "all") params.set("lev", tab);
  if (caseId) params.set("case", caseId);
  return params;
}
