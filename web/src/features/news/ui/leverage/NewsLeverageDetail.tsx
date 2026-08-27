import { ORDER_STATE_NOTE, policyRuleZh, stopVerified, type TradingOrder } from "@features/trading";
import { newsOiPath, tradingPath } from "@shared/routing/paths";
import { KeyValue, KeyValueRow } from "@shared/ui/KeyValue";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { NewsQuote } from "../../api/newsQueries";
import {
  leveragePlanes,
  leverageTimeline,
  leverageTrace,
  triggerLabel,
  type LeverageCase,
} from "../../model/leverageCases";
import { NewsTechnical } from "../chrome/NewsChrome";
import { NewsQuotePrice } from "../chrome/NewsQuoteValue";

import { DECISION_LABEL, EVIDENCE_LABEL, PHASE_LABEL, PLANE_LABEL } from "./leverageChrome";

import "./newsLeverageDetail.css";

type Plane = "t0" | "now" | "out";

/**
 * One case as a document: what was decided, what would falsify it, the three planes, the evidence, the
 * timeline and the capital loop — then the raw frame behind a fold.
 *
 * The three planes are separate tabs rather than one table on purpose. A frozen cutoff figure and a live
 * quote in adjacent cells is the single most misleading thing this page could draw: it invites a reader to
 * subtract one from the other and call the difference a return. Each plane names its own moment, and the
 * page never computes across them.
 */
export function NewsLeverageDetail({
  horizon,
  item,
  permission,
  quote,
  symbolHref,
}: {
  /** The mandate's forced-close window, already formatted. `—` when the status read has not answered. */
  horizon: string;
  item: LeverageCase;
  /**
   * What this strategy is allowed to do — `paper`, `shadow`, `live_reviewed` — from the strategy's own
   * published config. The artifact puts it beside the verdict because "LONG" and "LONG, on paper" are
   * different claims and only one of them can move money.
   */
  permission: string | null;
  quote: NewsQuote | undefined;
  symbolHref: string;
}) {
  const [plane, setPlane] = useState<Plane>("t0");
  const order = item.entry.kind === "order" ? item.entry.value : null;
  const planes = leveragePlanes(item, quote?.price ?? null, Date.now());
  const timeline = leverageTimeline(item);
  const gaps = item.evidence
    .filter((row) => row.status === "missing")
    .map((row) => row.label)
    .join(" · ");

  return (
    <section aria-label={`案例 ${item.base}`} className="news-leverage-detail">
      <header className="news-leverage-detail-head">
        <Link className="news-leverage-detail-symbol" to={symbolHref}>
          {item.base}
        </Link>
        <small>{item.underlyingKey}</small>
        <span className="news-leverage-regime">{item.regime}</span>
        <span className="news-leverage-detail-verdict">
          <b data-decision={item.decision}>{DECISION_LABEL[item.decision].big}</b>
          {permission ? (
            <span className="news-leverage-permission" title="该策略被允许做到哪一步">
              {permission.toUpperCase()}
            </span>
          ) : null}
          <span className="news-leverage-phase" data-phase={item.phase}>
            {PHASE_LABEL[item.phase]}
          </span>
        </span>
      </header>

      <div className="news-leverage-thesis">
        <div>
          <small>{setupLabel(item)}</small>
          <p>{item.why}</p>
          <small>失效条件 · INVALIDATION</small>
          <p>{invalidationSentence(item, order)}</p>
        </div>
        {/* The artifact's five rows. `case_id` used to hold the third slot and is now in the trace behind
            the fold: it is an identifier, not an answer, and it was the one row here nobody reads. */}
        <KeyValue className="news-leverage-kv">
          <KeyValueRow k="策略" v={item.strategyLabel} />
          <KeyValueRow k="规则" v={item.rule} />
          <KeyValueRow k="预期窗口" v={horizon} />
          <KeyValueRow k="触发" v={`${triggerLabel(item.triggerKind)} · ${item.age}`} />
          <KeyValueRow k="数据缺口" v={gaps || "—"} />
        </KeyValue>
      </div>

      <div className="news-leverage-planes">
        {/* Underline tabs, not the page's segmented pill: the pill above chooses which cases the list
            shows, this chooses which moment the case is described at, and the artifact keeps the two
            shapes apart so a reader never mistakes one control for the other. */}
        <div aria-label="事实平面" className="news-leverage-plane-tabs" role="tablist">
          {(Object.keys(PLANE_LABEL) as Plane[]).map((value) => (
            <button
              aria-selected={plane === value}
              className="news-leverage-plane-tab"
              data-active={plane === value || undefined}
              key={value}
              onClick={() => setPlane(value)}
              role="tab"
              type="button"
            >
              {PLANE_LABEL[value].label}
            </button>
          ))}
        </div>
        <small>{PLANE_LABEL[plane].note}</small>
      </div>
      <div className="news-leverage-plane-items">
        {planes[plane].map((cell) => (
          <div key={cell.key}>
            <small>{cell.label}</small>
            {cell.key === "quote" && quote ? <NewsQuotePrice quote={quote} /> : <b>{cell.value}</b>}
            {cell.note ? <small>{cell.note}</small> : null}
          </div>
        ))}
      </div>

      <div className="news-leverage-evidence">
        <small>证据矩阵 · 只有四种状态：支持 / 冲突 / 缺失 / 不适用</small>
        {item.evidence.map((row) => (
          <div key={row.key}>
            <small>{row.label}</small>
            <span data-status={row.status}>{EVIDENCE_LABEL[row.status]}</span>
            <small>{row.note}</small>
          </div>
        ))}
      </div>

      <div className="news-leverage-columns">
        <div className="news-leverage-timeline">
          <small>时间线 · TRIGGER → STRATEGY → CASE → ORDER</small>
          {timeline.map((step) => (
            <div key={step.key}>
              <span aria-hidden data-tone={step.tone} />
              <code>{step.at}</code>
              <span>
                <b>{step.label}</b>
                <span> · {step.note}</span>
              </span>
            </div>
          ))}
        </div>
        <div className="news-leverage-capital">
          <small>资本闭环 · 查看，不下单</small>
          {order ? (
            <>
              <div className="news-leverage-capital-head">
                <code>{order.order_id}</code>
                <span data-state={order.state}>{order.state}</span>
              </div>
              <KeyValue>
                <KeyValueRow
                  k="订单"
                  v={`${order.mode} ${order.side === "buy" ? "买入" : "卖出"} ${order.quantity} @ ${order.entry_reference}`}
                />
                <KeyValueRow
                  k="止损"
                  v={`${order.stop_price}${stopVerified(order) ? " · 原生止损已证明" : " · 保护未证明"}`}
                />
                <KeyValueRow k="状态说明" v={ORDER_STATE_NOTE[order.state] ?? order.state} />
              </KeyValue>
              <Link to={tradingPath()}>打开模拟仓 ›</Link>
            </>
          ) : (
            <p>
              {/*
               * `item.rule` is the ledger's raw reason everywhere else on this pane — the timeline note and
               * the 策略 · 规则 cell both print it verbatim — so it is translated in exactly one place, and a
               * case with no recorded reason says that rather than rendering `未成单：—。`
               */}
              {item.decision === "pending"
                ? "判定未完成，不形成资本意图——永不预下单。"
                : item.rule === "—"
                  ? "未成单：账本没有记录停在哪条规则上。"
                  : `未成单：${policyRuleZh(item.rule)}。`}
            </p>
          )}
        </div>
      </div>

      {/*
       * Two columns behind the fold, as the artifact draws them: the provider's line on the left and the
       * ledger's own row on the right. The trace was nowhere on this page before — the pane translated
       * every field into a sentence and then had no way to show what it had translated *from*.
       */}
      <NewsTechnical summary="原始证据与技术详情">
        <div className="news-leverage-raw">
          <small>供应商原帧</small>
          {/* The frame page is bounded and the ledger is not; a case older than the page has no wire line
              here, and saying which page it is on beats printing nothing. */}
          <code>
            {item.event?.leader_title ??
              (item.triggerKind === "oi"
                ? "原帧不在本页帧里（帧按页取）——在 OI 遥测审计上完整"
                : `${triggerLabel(item.triggerKind)}触发的案例：这条通道没有遥测帧，原始证据在事件流上`)}
          </code>
          <div className="news-leverage-raw-links">
            <Link to={newsOiPath()}>在 OI 遥测审计中查看 ›</Link>
            <Link to={symbolHref}>代币页 {item.base} ›</Link>
          </div>
        </div>
        <div className="news-leverage-trace">
          <small>判定痕迹 · CASE_TRACE</small>
          <KeyValue>
            {leverageTrace(item).map(([key, value]) => (
              <KeyValueRow k={key} key={key} v={value} />
            ))}
          </KeyValue>
        </div>
      </NewsTechnical>
    </section>
  );
}

/** The heading over the sentence changes with what the sentence has to answer. */
function setupLabel(item: LeverageCase): string {
  if (item.decision === "no_trade") return "为什么不交易 · NAMED RULE";
  if (item.decision === "pending") return "还差什么 · CONTEXT PENDING";
  return "为什么现在 · SETUP";
}

/**
 * What would end this case, from the ledger rather than from a sentence nobody wrote.
 *
 * The stop is only described as *working* when the ledger has proven it. `stopVerified` recognises exactly
 * `OPEN` — the one state with both a filled position and a read-back reduce-only stop covering it — and
 * every other state is a frozen intent. Telling an operator that crossing `stop_price` will trigger a
 * native stop on a `PREPARED`, `ACKNOWLEDGED` or `UNPROTECTED` order is false risk assurance on precisely
 * the rows that may carry exposure with no working protection (#185 P0-3).
 *
 * A `no_trade` case has nothing to invalidate — there is no position — and saying so is more useful than
 * leaving the field blank, which reads as missing data rather than as an answer.
 */
function invalidationSentence(item: LeverageCase, order: TradingOrder | null): string {
  if (item.decision === "no_trade") return "—（no_trade 无持仓可失效）";
  if (!order) return "判断形成后给出。";
  const hold =
    order.must_close_at_ms == null ? "最长持有从首笔成交起算，尚未起算" : "或最长持有到期强制平仓";
  return stopVerified(order)
    ? `价格穿过 ${order.stop_price} 触发原生止损，${hold}。`
    : `预设止损 ${order.stop_price}；账本尚未证明交易所持有对应的原生止损（${order.state}），${hold}。`;
}
