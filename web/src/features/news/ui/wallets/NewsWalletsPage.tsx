import { EmptyNote } from "@shared/ui/EmptyNote";
import { Metric, MetricRow } from "@shared/ui/Metric";
import { PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { SourceLine } from "@shared/ui/SourceLine";
import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  NEWS_WALLET_CARD_WINDOWS,
  useNewsWalletCardsWithToken,
  useNewsWalletsWithToken,
  type NewsWalletCard,
  type NewsWalletCardFilters,
  type NewsWalletCardTotal,
  type NewsWalletFillTotal,
  type NewsWalletFill,
  type NewsWalletRosterMember,
  type NewsWalletTapeState,
} from "../../api/newsQueries";
import { clockTime, displayTime, formatCount, optionalTime } from "../../model/newsLabels";
import { formatBps, formatPrice, priceTone } from "../../model/newsPrice";
import {
  nextWalletParams,
  parseWalletFilters,
  WALLET_CARD_FILTERS,
  walletHistoryPath,
  walletBasisLabel,
  walletCardLabel,
  walletCardMeasure,
  walletCardSubject,
  walletCardTitle,
  walletFillLabel,
  walletFillQuantity,
  walletTransactionUrl,
} from "../../model/walletFacts";
import { NewsPageHeader } from "../chrome/NewsChrome";

import "./newsWallets.css";

/** Buy candidates and their evidence lead; tape health is an independent supporting read. */
export function NewsWalletsPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = parseWalletFilters(searchParams);
  const walletsQuery = useNewsWalletsWithToken(token);
  const cardsQuery = useNewsWalletCardsWithToken(token, filters);
  const tape = walletsQuery.data;
  const setFilters = (next: NewsWalletCardFilters) =>
    setSearchParams(nextWalletParams(next), { replace: true });
  const filterAddresses = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    setFilters({
      ...filters,
      walletAddress: String(values.get("wallet_address") ?? "")
        .trim()
        .toLowerCase(),
      tokenAddress: String(values.get("token_address") ?? "")
        .trim()
        .toLowerCase(),
    });
  };

  return (
    <PageShell archetype="scan" className="news-wallets-shell" label="链上钱包">
      <NewsPageHeader
        subtitle="先看谁在买、买入阶段与后续变化。名单是观察范围，质量榜与持仓规模分别展示；买入候选是否通知、历史是否完整、价格何时取得，都保留各自依据。"
        title="链上钱包"
      />
      <div className="news-wallets-body">
        <section aria-label="钱包卡片" className="news-wallets-panel">
          <div className="news-wallets-toolbar">
            <b>{filters.walletAddress || filters.tokenAddress ? "观察时间线" : "买入研究"}</b>
            <div aria-label="按类型筛选" className="news-wallets-windows" role="group">
              {WALLET_CARD_FILTERS.map((kind) => (
                <button
                  aria-pressed={kind === filters.kind}
                  className="news-wallets-window"
                  data-active={kind === filters.kind || undefined}
                  key={kind}
                  onClick={() => setFilters({ ...filters, kind })}
                  type="button"
                >
                  {kind === "all" ? "全部" : walletCardLabel(kind)}
                </button>
              ))}
            </div>
            <div aria-label="按窗口筛选" className="news-wallets-windows" role="group">
              {NEWS_WALLET_CARD_WINDOWS.map((window) => (
                <button
                  aria-pressed={window === filters.window}
                  className="news-wallets-window"
                  data-active={window === filters.window || undefined}
                  key={window}
                  onClick={() => setFilters({ ...filters, window })}
                  type="button"
                >
                  {window}
                </button>
              ))}
            </div>
          </div>
          <form
            className="news-wallets-filters"
            key={`${filters.walletAddress}:${filters.tokenAddress}`}
            onSubmit={filterAddresses}
          >
            <label>
              钱包地址
              <input
                defaultValue={filters.walletAddress}
                name="wallet_address"
                pattern="0x[0-9a-fA-F]{40}"
                placeholder="0x… 精确地址"
                spellCheck={false}
              />
            </label>
            <label>
              代币合约
              <input
                defaultValue={filters.tokenAddress}
                name="token_address"
                pattern="0x[0-9a-fA-F]{40}"
                placeholder="0x… 精确合约"
                spellCheck={false}
              />
            </label>
            <button className="news-wallets-window" type="submit">
              筛选
            </button>
            {filters.walletAddress || filters.tokenAddress ? (
              <button
                className="news-wallets-window"
                onClick={() => setFilters({ ...filters, walletAddress: "", tokenAddress: "" })}
                type="button"
              >
                清除地址
              </button>
            ) : null}
          </form>
          <p className="news-wallets-note">
            按观察时间从新到旧，包含未发送候选。价格变化的基准见各行记录，成交均价单列。
            {cardsQuery.data
              ? ` ${displayTime(cardsQuery.data.window_from_ms)} → ${displayTime(cardsQuery.data.window_to_ms)}`
              : ""}
          </p>
          <CardsPanel filters={filters} query={cardsQuery} />
        </section>

        {walletsQuery.isLoading && !tape ? (
          <PageState.TileSkeleton label="正在读取链上钱包状态" tiles={4} />
        ) : null}
        {walletsQuery.isError && !tape ? (
          <PageState.Error error={walletsQuery.error} onRetry={() => void walletsQuery.refetch()} />
        ) : null}
        {tape ? (
          <PageState.Stale
            failedRefresh={
              walletsQuery.isError ? "链上钱包状态刷新失败，下面仍是上次读取的结果。" : undefined
            }
            onRetry={() => void walletsQuery.refetch()}
            updating={walletsQuery.isFetching}
          >
            <div className="news-wallets-body">
              <TapeTiles cards={tape.cards} fills={tape.fills} tape={tape.tape ?? null} />
              <section aria-label="跟踪名单" className="news-wallets-panel">
                <div className="news-wallets-toolbar">
                  <b>跟踪名单</b>
                  <small>
                    版本 {tape.roster.roster_version} · {tape.roster.members.length} 个地址 ·{" "}
                    {optionalTime(tape.roster.taken_at_ms)} 取得
                  </small>
                </div>
                <p className="news-wallets-note">
                  质量榜按历史统计筛选，大户榜按持仓成本排序。进入名单不代表已验证的盈利能力；点击钱包查看其观察时间线。
                </p>
                {tape.roster.members.length === 0 ? (
                  <EmptyNote>还没有名单版本：链上钱包任务未开启或第一次刷新尚未完成。</EmptyNote>
                ) : (
                  <RosterTable filters={filters} members={tape.roster.members} />
                )}
              </section>
            </div>
          </PageState.Stale>
        ) : null}
        <SourceLine
          note="候选与 +15m/+1h/+4h 价格回执独立读取；名单和运行状态失败不会隐藏候选"
          path="GET /api/news/wallets/cards → cards[] · fills[] ｜ GET /api/news/wallets → roster · tape · fills[] · cards[]"
        />
      </div>
    </PageShell>
  );
}

/**
 * The day in four figures: what was stored, what was sent, what nothing could price, and where the tape is.
 *
 * The unpriced share is a figure rather than a warning. A trade whose cash leg was not the pinned
 * stablecoin keeps its quantity and loses only its dollar value, which is a fact about the pool it went
 * through — the rules simply do not fire on it.
 */
function TapeTiles({
  cards,
  fills,
  tape,
}: {
  cards: readonly NewsWalletCardTotal[];
  fills: readonly NewsWalletFillTotal[];
  tape: NewsWalletTapeState | null;
}) {
  const totalFills = fills.reduce((sum, row) => sum + row.fills, 0);
  /*
   * A transfer out has no cash leg by construction, so counting it as unpriced would report the tape's own
   * classification as a pricing failure. Numerator and denominator are the same rows: the trades.
   */
  const trades = fills.filter((row) => row.kind !== "transfer_out");
  const priceable = trades.reduce((sum, row) => sum + row.fills, 0);
  const unpriced = trades.reduce((sum, row) => sum + row.unpriced, 0);
  const totalCards = cards.reduce((sum, row) => sum + row.cards, 0);
  const sent = cards.reduce((sum, row) => sum + row.sent, 0);
  return (
    <MetricRow columns={4} label="链上钱包 24 小时">
      <Metric
        caption={
          fills
            .map((row) => `${walletFillLabel(row.kind)} ${formatCount(row.fills)}`)
            .join(" · ") || "无成交"
        }
        eyebrow="FILLS 24H"
        value={formatCount(totalFills)}
      />
      <Metric
        caption={
          cards
            .map((row) => `${walletCardLabel(row.kind)} ${formatCount(row.cards)}`)
            .join(" · ") || "无卡片"
        }
        eyebrow="CARDS 24H"
        note={`已送达 ${formatCount(sent)}`}
        tone={totalCards ? "accent" : "plain"}
        value={formatCount(totalCards)}
      />
      <Metric
        caption="未取得可归属美元现金腿的成交"
        eyebrow="UNPRICED"
        note={`${formatCount(unpriced)} / ${formatCount(priceable)} 笔`}
        value={priceable ? `${((unpriced / priceable) * 100).toFixed(1)}%` : "—"}
      />
      <Metric
        caption={tape ? `高水位区块 ${formatCount(tape.high_water_block)}` : "任务未运行"}
        eyebrow="TAPE"
        note={
          tape
            ? `丢弃 ${formatCount(tape.ignored_inbound_total + tape.unknown_total)} 笔 · ${optionalTime(tape.last_success_at_ms)}`
            : undefined
        }
        title={tape?.last_error ?? undefined}
        tone={tape && tape.last_outcome !== "success" ? "caution" : "plain"}
        value={tape ? tape.last_outcome || "—" : "—"}
      />
    </MetricRow>
  );
}

/**
 * Who is followed and why. Win rate is shown and is deliberately not a criterion: over the addresses with
 * five or more closes its rank correlation with realized P&L was 0.31, and four of the nine above 0.6 were
 * losing money (#572 §3.2). The two ranks are two separate lists — quality by realized P&L, whale by open
 * cost — and a wallet can hold one, both or neither rank while still being followed.
 */
function RosterTable({
  filters,
  members,
}: {
  filters: NewsWalletCardFilters;
  members: readonly NewsWalletRosterMember[];
}) {
  return (
    <div className="news-wallets-scroll">
      <table className="news-wallets-table">
        <thead>
          <tr>
            <th scope="col">Handle</th>
            <th scope="col">粉丝</th>
            <th scope="col">质量榜</th>
            <th scope="col">大户榜</th>
            <th scope="col">已实现盈亏</th>
            <th scope="col">Profit factor</th>
            <th scope="col">平仓数</th>
            <th scope="col">胜率</th>
            <th scope="col">持仓成本</th>
          </tr>
        </thead>
        <tbody>
          {members.map((member) => (
            <tr key={member.wallet}>
              <th scope="row" title={member.wallet}>
                <Link
                  className="news-wallets-kind"
                  to={walletHistoryPath({ ...filters, walletAddress: member.wallet })}
                >
                  {member.handle || member.wallet.slice(0, 10)}
                </Link>
              </th>
              <td>{formatCount(member.followers)}</td>
              <td>{member.rank_quality ?? "—"}</td>
              <td>{member.rank_whale ?? "—"}</td>
              <td data-tone={priceTone(member.realized_pnl)}>
                {formatCount(Math.round(member.realized_pnl))}
              </td>
              <td>{member.profit_factor == null ? "—" : member.profit_factor.toFixed(2)}</td>
              <td>{formatCount(member.closed_trades)}</td>
              <td>{`${(member.win_rate * 100).toFixed(0)}%`}</td>
              <td>{formatCount(Math.round(member.open_cost))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Candidate and delivery state are independent: unsent rows keep their price evidence. */
function CardsPanel({
  filters,
  query,
}: {
  filters: NewsWalletCardFilters;
  query: ReturnType<typeof useNewsWalletCardsWithToken>;
}) {
  if (query.isError && !query.data) {
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (!query.data) {
    return <PageState.Loading label="正在读取钱包卡片" layout="panel" rows={6} />;
  }
  return (
    <PageState.Stale
      failedRefresh={query.isError ? "观察刷新失败，仍显示上次读取的结果。" : undefined}
      onRetry={() => void query.refetch()}
      updating={query.isFetching}
    >
      {query.data.cards.length === 0 ? (
        <EmptyNote>当前窗口与筛选下没有观察记录。</EmptyNote>
      ) : (
        <div className="news-wallets-scroll">
          <table className="news-wallets-table" data-table="cards">
            <thead>
              <tr>
                <th scope="col">时间</th>
                <th scope="col">类型</th>
                <th scope="col">钱包</th>
                <th scope="col">标的</th>
                <th scope="col">阶段 / 规模</th>
                <th scope="col">已计价金额</th>
                <th scope="col">已计价均价</th>
                <th scope="col">观察价</th>
                <th scope="col">推送</th>
                <th scope="col">+15m</th>
                <th scope="col">+1h</th>
                <th scope="col">+4h</th>
              </tr>
            </thead>
            <tbody>
              {query.data.cards.map((card) => (
                <CardRow card={card} filters={filters} key={card.item_id} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {filters.walletAddress && filters.tokenAddress ? (
        <WalletFillsTable fills={query.data.fills} limit={query.data.limit} />
      ) : null}
    </PageState.Stale>
  );
}

function WalletFillsTable({ fills, limit }: { fills: readonly NewsWalletFill[]; limit: number }) {
  return (
    <section aria-label="交易流水">
      <div className="news-wallets-toolbar">
        <b>交易流水</b>
      </div>
      <p className="news-wallets-note">
        同钱包与代币在当前窗口内的买入、卖出与转出，不受卡片类型与提醒门槛影响。按链上顺序从新到旧，最多{" "}
        {limit} 笔；仅覆盖已保留流水。
      </p>
      {fills.length === 0 ? (
        <EmptyNote>当前窗口内没有保留的交易流水。</EmptyNote>
      ) : (
        <div className="news-wallets-scroll">
          <table className="news-wallets-table">
            <thead>
              <tr>
                <th scope="col">时间</th>
                <th scope="col">动作</th>
                <th scope="col">数量</th>
                <th scope="col">已计价金额</th>
                <th scope="col">区块 / 日志</th>
                <th scope="col">交易</th>
              </tr>
            </thead>
            <tbody>
              {fills.map((fill) => {
                const transactionUrl = walletTransactionUrl(fill);
                return (
                  <tr key={`${fill.chain_id}:${fill.tx_hash}:${fill.log_index}`}>
                    <td title={displayTime(fill.event_at_ms)}>{clockTime(fill.event_at_ms)}</td>
                    <td>{walletFillLabel(fill.kind)}</td>
                    <td>{walletFillQuantity(fill)}</td>
                    <td>{fill.usd == null ? "未知" : formatPrice(fill.usd)}</td>
                    <td>
                      {formatCount(fill.block_number)} / {fill.log_index}
                    </td>
                    <td>
                      {transactionUrl ? (
                        <a
                          className="news-wallets-kind"
                          href={transactionUrl}
                          rel="noopener noreferrer"
                          target="_blank"
                          title={fill.tx_hash}
                        >
                          {fill.tx_hash.slice(0, 10)}…
                        </a>
                      ) : (
                        <span title={fill.tx_hash}>{fill.tx_hash.slice(0, 10)}…</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function CardRow({ card, filters }: { card: NewsWalletCard; filters: NewsWalletCardFilters }) {
  const lines = card.digest_lines ?? [];
  return (
    <>
      <tr data-kind={card.kind}>
        <td title={displayTime(card.event_at_ms)}>{clockTime(card.event_at_ms)}</td>
        <td>
          <Link
            className="news-wallets-kind"
            title={walletCardTitle(card.kind)}
            to={`/news/market/${card.item_id}`}
          >
            {walletCardLabel(card.kind)}
            {card.tone === "late" ? " · 偏晚" : ""}
          </Link>
        </td>
        <td>
          {card.wallet ? (
            <Link
              className="news-wallets-kind"
              title={card.wallet}
              to={walletHistoryPath({ ...filters, walletAddress: card.wallet })}
            >
              {card.handle || card.wallet.slice(0, 10)}
            </Link>
          ) : (
            "—"
          )}
        </td>
        <td>
          {card.token ? (
            <Link
              className="news-wallets-kind"
              title={card.token}
              to={walletHistoryPath({ ...filters, tokenAddress: card.token })}
            >
              {walletCardSubject(card)}
            </Link>
          ) : (
            walletCardSubject(card)
          )}
        </td>
        <td>{walletCardMeasure(card)}</td>
        <td>{formatPrice(card.usd ?? card.position_usd)}</td>
        <td>{formatPrice(card.entry_price)}</td>
        <td>{formatPrice(card.mark_price)}</td>
        <td>{card.delivery_state ?? "未发送"}</td>
        <Outcome bps={card.return_15m_bps} source={card.outcome_15m_source} />
        <Outcome bps={card.return_1h_bps} source={card.outcome_1h_source} />
        <Outcome bps={card.return_4h_bps} source={card.outcome_4h_source} />
      </tr>
      {card.kind !== "digest" ? (
        <tr className="news-wallets-lines" data-kind={card.kind}>
          <td colSpan={12}>
            <div className="news-wallets-evidence">
              {card.selection_reason ? <span>记录原因：{card.selection_reason}</span> : null}
              {card.kind === "buy" ? (
                <>
                  <span>窗口买入 {card.buy_count == null ? "未记录" : `${card.buy_count} 笔`}</span>
                  <span>
                    未计价 {card.unpriced_buys == null ? "未记录" : `${card.unpriced_buys} 笔`}
                  </span>
                  <span>观察于 {optionalTime(card.observed_at_ms)}</span>
                  <span>历史覆盖自 {optionalTime(card.history_from_ms)}</span>
                </>
              ) : card.kind === "exit" ? (
                <span>{walletBasisLabel(card.basis)}</span>
              ) : null}
              <span>价格基准：{card.price_reference || "未记录"}</span>
              {card.wallet && card.token ? (
                <Link
                  className="news-wallets-kind"
                  to={walletHistoryPath({
                    ...filters,
                    walletAddress: card.wallet,
                    tokenAddress: card.token,
                  })}
                >
                  同钱包与代币的后续变化
                </Link>
              ) : null}
            </div>
          </td>
        </tr>
      ) : null}
      {lines.length ? (
        <tr className="news-wallets-lines" data-kind={card.kind}>
          <td colSpan={12}>
            <ol>
              {lines.map((line, index) => (
                <li key={index}>{line}</li>
              ))}
            </ol>
          </td>
        </tr>
      ) : null}
    </>
  );
}

/**
 * One horizon's receipt. Three states and they are three different facts: a number, "we looked and could
 * not price it", and "not due yet" — which is the absence of a row rather than a zero.
 */
function Outcome({
  bps,
  source,
}: {
  bps: number | null | undefined;
  source: string | null | undefined;
}) {
  if (bps == null) {
    return <td title={source ?? undefined}>{source === "unavailable" ? "无价" : "—"}</td>;
  }
  return (
    <td data-tone={priceTone(bps)} title={source ?? undefined}>
      {formatBps(bps)}
    </td>
  );
}
