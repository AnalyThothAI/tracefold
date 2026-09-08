import { KeyValue, KeyValueRow } from "@shared/ui/KeyValue";
import { Link, useLocation } from "react-router-dom";

import type { NewsWalletCard, NewsWalletCardFilters } from "../../api/newsQueries";
import { clockTime, displayTime, optionalTime } from "../../model/newsLabels";
import { formatBps, formatPrice, priceTone } from "../../model/newsPrice";
import {
  nextWalletParams,
  walletCardSubject,
  walletCardMeasure,
  walletOutcomeLabel,
  walletSelectionLabel,
} from "../../model/walletFacts";

export function WalletResearchRow({
  card,
  filters,
  open,
  onOpen,
}: {
  card: NewsWalletCard;
  filters: NewsWalletCardFilters;
  open: boolean;
  onOpen: () => void;
}) {
  const location = useLocation();
  const hour = card.outcomes.find((outcome) => outcome.horizon === "1h");
  const timeline = nextWalletParams({
    ...filters,
    cursor: undefined,
    kind: "buy",
    chainId: card.chain_id,
    walletAddress: card.wallet,
    tokenAddress: card.token,
    segmentKey: card.segment_key,
    view: "observations",
  });
  return (
    <article className="news-wallet-research-row">
      <button
        className="news-wallet-research-trigger"
        aria-expanded={open}
        onClick={onOpen}
        type="button"
      >
        <span className="news-wallet-research-identity">
          <b>
            {card.handle || "全名单"} <span>· {walletCardSubject(card)}</span>
          </b>
          <small>
            链 {card.chain_id} ·{" "}
            {card.wallet ? `${card.wallet.slice(0, 6)}…${card.wallet.slice(-4)}` : "窗口摘要"}
          </small>
        </span>
        <span>
          <small>阶段 / 动作</small>
          <b>{walletCardMeasure(card)}</b>
        </span>
        <span>
          <small>{card.kind === "buy" ? "本段累计已计价" : "已计价金额"}</small>
          <b>{card.usd == null ? "未取得金额" : `$${formatPrice(card.usd)}`}</b>
          <small>
            {card.buy_count == null ? "" : `${card.buy_count} 笔买入`}
            {card.unpriced_buys ? ` · ${card.unpriced_buys} 笔未计价` : ""}
          </small>
        </span>
        <span>
          <small>观察后 1 小时</small>
          <b data-tone={hour?.status === "measured" ? priceTone(hour.return_bps) : undefined}>
            {hour?.status === "measured"
              ? formatBps(hour.return_bps)
              : hour
                ? walletOutcomeLabel(hour)
                : "不适用"}
          </b>
          {card.price_status !== "verified" && card.kind !== "digest" ? (
            <small className="news-wallet-price-gap">
              {card.price_status === "missing" ? "缺观察基准" : "观察价格身份未核实"}
            </small>
          ) : null}
        </span>
        <span>
          <small>{clockTime(card.event_at_ms)}</small>
          <b>{open ? "收起依据" : "查看依据"}</b>
          <small>{card.observation_count} 次观察</small>
        </span>
      </button>
      {open ? (
        <div className="news-wallet-research-detail">
          <p>
            本行金额和后续变化对应同一条观察，累计金额取
            {filters.view === "observations" ? "该次观察时的快照" : "本段最新快照"}
            。观察后价格变化不代表钱包盈亏或跟单收益。
          </p>
          <KeyValue>
            {[
              ["钱包地址", card.wallet || "不适用"],
              ["代币合约", card.token || "不适用"],
              ["观察时间", optionalTime(card.observed_at_ms)],
              ["历史覆盖自", optionalTime(card.history_from_ms)],
              ["已计价成交均价", formatPrice(card.entry_price)],
              ["观察价 · USD / token", formatPrice(card.mark_price)],
              ["观察价格来源", card.mark_source ?? "未记录"],
              ["来源报价时间", optionalTime(card.price_source_at_ms)],
              ["记录原因", walletSelectionLabel(card.selection_reason)],
              ["通知回执", card.delivery_state ?? "未发送"],
            ].map(([k, v]) => (
              <KeyValueRow key={k} k={k} v={v} />
            ))}
          </KeyValue>
          {card.outcomes.length ? (
            <section aria-label="观察后价格" className="news-wallet-outcomes">
              {card.outcomes.map((outcome) => (
                <div key={outcome.horizon}>
                  <small>观察后 {outcome.horizon}</small>
                  <b
                    data-tone={
                      outcome.status === "measured" ? priceTone(outcome.return_bps) : undefined
                    }
                  >
                    {outcome.status === "measured"
                      ? formatBps(outcome.return_bps)
                      : walletOutcomeLabel(outcome)}
                  </b>
                  <small>目标 {optionalTime(outcome.target_at_ms)}</small>
                  <small>实际采样 {optionalTime(outcome.sampled_at_ms)}</small>
                  <small>
                    基准 {formatPrice(outcome.reference_price)} → {formatPrice(outcome.price)}
                  </small>
                  <small>{outcome.source ?? "尚无回执"}</small>
                </div>
              ))}
            </section>
          ) : null}
          {card.digest_lines?.length ? (
            <ol>
              {card.digest_lines.map((line, index) => (
                <li key={index}>{line}</li>
              ))}
            </ol>
          ) : null}
          <div className="news-wallet-research-links">
            {card.kind === "buy" && card.segment_key && filters.view !== "observations" ? (
              <Link to={`/news/wallets?${timeline}`}>展开本段 {card.observation_count} 次观察</Link>
            ) : null}
            {card.wallet && card.token ? (
              <Link
                to={`/news/wallets?${nextWalletParams({
                  ...filters,
                  cursor: undefined,
                  view: "observations",
                  kind: "all",
                  chainId: card.chain_id,
                  walletAddress: card.wallet,
                  tokenAddress: card.token,
                  segmentKey: undefined,
                })}`}
              >
                同钱包与代币的买卖 / 转出
              </Link>
            ) : null}
            <Link
              state={{ researchFrom: location.pathname + location.search }}
              to={`/news/market/${card.item_id}`}
            >
              原始观察与交易依据
            </Link>
          </div>
          <small className="news-wallet-anchor">
            本行基准观察 <code>{card.item_id}</code> · {displayTime(card.event_at_ms)}
          </small>
        </div>
      ) : null}
    </article>
  );
}
