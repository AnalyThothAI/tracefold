import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";
import { Link, useSearchParams } from "react-router-dom";

import {
  NEWS_WALLET_CARD_WINDOWS,
  useNewsWalletCardsWithToken,
  useNewsWalletsWithToken,
  type NewsWalletCard,
  type NewsWalletCardTotal,
  type NewsWalletFillTotal,
  type NewsWalletRosterMember,
  type NewsWalletTapeState,
} from "../../api/newsQueries";
import { clockTime, displayTime, formatCount, optionalTime } from "../../model/newsLabels";
import { formatBps, formatPrice, priceTone } from "../../model/newsPrice";
import {
  nextWalletParams,
  parseWalletWindow,
  walletBasisLabel,
  walletCardLabel,
  walletCardMeasure,
  walletCardSubject,
  walletCardTitle,
  walletFillLabel,
} from "../../model/walletFacts";
import { NewsEmptyNote, NewsPageHeader, NewsPageShell } from "../chrome/NewsChrome";
import { NewsSourceLine } from "../chrome/NewsSourceLine";

import "./newsWallets.css";

/**
 * 链上钱包 — what the Robinhood Chain wallet tape follows, reads and sends (#572 PR-3).
 *
 * The market list already publishes a wallet observation the way it publishes every other market kind.
 * This page answers the question that surface cannot: what the *tape* is doing. Which wallets it follows
 * and on which of the two lists, how far it has read the chain, what it stored and how much of it nothing
 * could price, and — for the cards it opened — what the token did one and four hours after a reader was
 * told.
 *
 * **Two independent reads, two independent failures.** The header and the roster come from
 * `/api/news/wallets`; the card table comes from `/api/news/wallets/cards` on its own window. A slow or
 * failing card table leaves the roster and the tape's position exactly where they are, and the reverse
 * holds too — neither read is a precondition for the other, because neither answers the other's question.
 *
 * **Every receipt is published, and none of them is a gate.** The +1h and +4h columns are #572 §11's
 * effect receipt: nothing in the code reads them, a negative number is an answer rather than a fault, and
 * a horizon nothing could price says so instead of showing a zero.
 */
export function NewsWalletsPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const window = parseWalletWindow(searchParams.get("window"));
  const walletsQuery = useNewsWalletsWithToken(token);
  const cardsQuery = useNewsWalletCardsWithToken(token, window);
  const tape = walletsQuery.data;

  return (
    <NewsPageShell archetype="scan" className="news-wallets-shell" label="链上钱包">
      <NewsPageHeader
        subtitle="Robinhood Chain 上被跟踪钱包的成交由链上日志推导：名单按原站的已实现盈亏与 profit factor 构建，退出卡与拥挤卡由规则决定，摘要每四小时一次。这一页只答「跟谁、读到哪、存了什么、发了什么、事后值多少」。"
        title="链上钱包"
      />

      {walletsQuery.isLoading && !tape ? (
        <div className="news-wallets-body">
          <PageState.TileSkeleton label="正在读取链上钱包状态" tiles={4} />
          <PageState.Loading label="正在读取名单" layout="panel" rows={6} />
        </div>
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
              {tape.roster.members.length === 0 ? (
                <NewsEmptyNote>
                  还没有名单版本：链上钱包任务未开启或第一次刷新尚未完成。
                </NewsEmptyNote>
              ) : (
                <RosterTable members={tape.roster.members} />
              )}
            </section>

            <section aria-label="钱包卡片" className="news-wallets-panel">
              <div className="news-wallets-toolbar">
                <div aria-label="按窗口筛选" className="news-wallets-windows" role="group">
                  {NEWS_WALLET_CARD_WINDOWS.map((option) => (
                    <button
                      aria-pressed={option === window}
                      className="news-wallets-window"
                      data-active={option === window || undefined}
                      key={option}
                      onClick={() => setSearchParams(nextWalletParams(option), { replace: true })}
                      type="button"
                    >
                      {option}
                    </button>
                  ))}
                </div>
                <small>
                  {cardsQuery.data
                    ? `${displayTime(cardsQuery.data.window_from_ms)} → ${displayTime(cardsQuery.data.window_to_ms)}`
                    : "正在读取"}
                </small>
              </div>
              <CardsPanel query={cardsQuery} />
            </section>

            <NewsSourceLine
              note="名单、tape 位置与两块计数来自同一次读取；卡片与 +1h/+4h 回执来自卡片读取，二者互不阻塞"
              path="GET /api/news/wallets → roster · tape · fills[] · cards[] ｜ GET /api/news/wallets/cards → cards[]"
            />
          </div>
        </PageState.Stale>
      ) : null}
    </NewsPageShell>
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
        note={`${fills.reduce((count, row) => count + row.wallets, 0)} 个地址 · ${fills.reduce((count, row) => count + row.tokens, 0)} 个代币`}
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
        caption="现金腿不是锚定稳定币的成交"
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
function RosterTable({ members }: { members: readonly NewsWalletRosterMember[] }) {
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
                {member.handle || member.wallet.slice(0, 10)}
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

/** The card table and its own three states; the header above it is untouched by any of them. */
function CardsPanel({ query }: { query: ReturnType<typeof useNewsWalletCardsWithToken> }) {
  if (query.isError && !query.data) {
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (!query.data) {
    return <PageState.Loading label="正在读取钱包卡片" layout="panel" rows={6} />;
  }
  if (query.data.cards.length === 0) {
    return <NewsEmptyNote>这个窗口里规则没有开出任何卡片。</NewsEmptyNote>;
  }
  return (
    <div className="news-wallets-scroll">
      <table className="news-wallets-table" data-table="cards">
        <thead>
          <tr>
            <th scope="col">时间</th>
            <th scope="col">类型</th>
            <th scope="col">Handle</th>
            <th scope="col">标的</th>
            <th scope="col">比例 / 规模</th>
            <th scope="col">口径</th>
            <th scope="col">金额</th>
            <th scope="col">推送</th>
            <th scope="col">+1h</th>
            <th scope="col">+4h</th>
          </tr>
        </thead>
        <tbody>
          {query.data.cards.map((card) => (
            <CardRow card={card} key={card.item_id} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CardRow({ card }: { card: NewsWalletCard }) {
  return (
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
      <td title={card.wallet || undefined}>{card.handle || "—"}</td>
      <td title={card.token || undefined}>{walletCardSubject(card)}</td>
      <td>{walletCardMeasure(card)}</td>
      <td>{card.kind === "exit" ? walletBasisLabel(card.basis) : "—"}</td>
      <td>
        {card.usd
          ? formatPrice(card.usd)
          : card.position_usd
            ? formatPrice(card.position_usd)
            : "—"}
      </td>
      {/* An open string the notification owner writes, printed as it is. */}
      <td>{card.delivery_state ?? "未开始"}</td>
      <Outcome bps={card.return_1h_bps} source={card.outcome_1h_source} />
      <Outcome bps={card.return_4h_bps} source={card.outcome_4h_source} />
    </tr>
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
