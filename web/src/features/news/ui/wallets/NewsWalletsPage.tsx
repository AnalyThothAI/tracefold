import { ActionButton } from "@shared/ui/ActionButton";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { Link, useSearchParams } from "react-router-dom";

import { NEWS_WALLET_HISTORY_RANGES, useNewsWalletEventsWithToken } from "../../api/newsQueries";
import { displayTime } from "../../model/newsLabels";
import {
  parseWalletEventFilters,
  walletDecimal,
  walletNotificationLabel,
  walletParticipations,
  walletReason,
  walletTokenAge,
} from "../../model/walletFacts";
import { NewsPageHeader } from "../chrome/NewsChrome";

import { WalletEventDetail } from "./WalletEventDetail";
import { WalletStatusBlock } from "./WalletStatusBlock";
import "./newsWallets.css";

export function NewsWalletsPage({ token }: { token: string }) {
  const [params, setParams] = useSearchParams();
  const filters = parseWalletEventFilters(params);
  const episodeId = params.get("episode") ?? "";
  const eventsQuery = useNewsWalletEventsWithToken(token, filters);
  const data = eventsQuery.data;
  const changeRange = (historyRange: string) => {
    const next = new URLSearchParams();
    if (historyRange !== "24h") next.set("history_range", historyRange);
    setParams(next);
  };

  return (
    <PageShell archetype="scan" className="news-wallets-shell" label="聪明钱警报">
      <NewsPageHeader title="聪明钱警报" subtitle="Robinhood Chain · 多钱包集中净买入" />
      <WalletStatusBlock token={token} />
      {episodeId ? <WalletEventDetail token={token} episodeId={episodeId} /> : null}
      <section className="news-wallets-panel" aria-label="集中净买入事件">
        <div className="news-wallets-toolbar">
          <div className="news-wallets-windows" role="group" aria-label="历史查询范围">
            {NEWS_WALLET_HISTORY_RANGES.map((range) => (
              <button
                type="button"
                key={range}
                className="news-wallets-window"
                aria-pressed={filters.historyRange === range}
                data-active={filters.historyRange === range || undefined}
                onClick={() => changeRange(range)}
              >
                {range}
              </button>
            ))}
          </div>
          <ActionButton size="sm" onClick={() => void eventsQuery.refetch()}>
            刷新事件
          </ActionButton>
        </div>
        <p className="news-wallets-note">
          30 分钟是唯一的触发窗口；历史范围用于查阅事件。一行一轮，包含静音与未发送事件。
        </p>
        {data ? (
          <p className="news-wallets-note">
            {displayTime(data.history_from_ms)} → {displayTime(data.history_to_ms)} · 全范围{" "}
            {data.totals.total} 轮 · 进行中 {data.totals.active} 轮 · 已发送 {data.totals.sent} 轮
          </p>
        ) : null}
        {eventsQuery.isError && !data ? (
          <PageState.Error error={eventsQuery.error} onRetry={() => void eventsQuery.refetch()} />
        ) : !data ? (
          <PageState.Loading label="正在读取集中净买入事件" layout="panel" rows={5} />
        ) : (
          <PageState.Stale
            failedRefresh={eventsQuery.isError ? "事件刷新失败，保留上次读取的事实。" : undefined}
            updating={eventsQuery.isFetching}
            onRetry={() => void eventsQuery.refetch()}
          >
            {data.events.length ? (
              <div className="news-wallets-scroll">
                <table className="news-wallets-table news-wallet-event-list">
                  <thead>
                    <tr>
                      <th>代币 / 链 / 触发时年龄</th>
                      <th>首次净买家</th>
                      <th>首次窗口净买入</th>
                      <th>触发 / 本轮状态</th>
                      <th>通知结果</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.events.map((event) => {
                      const first = event.initial_snapshot;
                      const primary = first.window;
                      const latest = event.latest_snapshot;
                      const otherSell = latest.window.members.some(
                        (member) => !member.qualified && member.net_usd?.startsWith("-"),
                      );
                      const incomplete = latest.window.members.some((member) =>
                        member.reasons.some((reason) =>
                          [
                            "transfer_out_incomplete",
                            "unpriced_trade",
                            "collection_gap",
                            "incomplete_monitoring_window",
                          ].includes(reason),
                        ),
                      );
                      const linkParams = new URLSearchParams(params);
                      linkParams.set("episode", event.episode_id);
                      return (
                        <tr key={event.episode_id}>
                          <td data-label="代币 / 链 / 触发时年龄">
                            <Link to={`/news/wallets?${linkParams}`}>
                              {event.token_symbol || event.token}
                            </Link>
                            <small>Robinhood Chain · {walletTokenAge(first)}</small>
                            <code>{event.token}</code>
                          </td>
                          <td data-label="首次净买家">
                            {primary.qualified_n} / {primary.required_n}
                            <small>{walletParticipations(primary)}</small>
                          </td>
                          <td data-label="首次窗口净买入">
                            ${walletDecimal(primary.net_usd)}
                            <small>30 分钟 · 入选地址</small>
                          </td>
                          <td data-label="触发 / 本轮状态">
                            {displayTime(event.triggered_at_ms)}
                            <small>
                              {event.ended_at_ms === null ? "本轮进行中" : "本轮已结束"}
                            </small>
                            {!latest.window.matched ? (
                              <small>当前人数或净额已不满足条件</small>
                            ) : null}
                            {otherSell ? <small>其他观察地址存在净卖出</small> : null}
                            {incomplete ? <small>存在未计价、转出或覆盖不足</small> : null}
                          </td>
                          <td data-label="通知结果">
                            {walletNotificationLabel(event.notification_state)}
                            {event.notification_reason ? (
                              <small>{walletReason(event.notification_reason)}</small>
                            ) : null}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyNote>当前历史范围没有集中净买入事件。</EmptyNote>
            )}
            <div className="news-wallets-toolbar">
              <small>
                本页 {data.events.length} 轮 · 全范围 {data.totals.total} 轮
              </small>
              {filters.cursor ? (
                <ActionButton size="sm" onClick={() => changeRange(filters.historyRange)}>
                  回到最新
                </ActionButton>
              ) : null}
              {data.next_cursor ? (
                <ActionButton
                  size="sm"
                  onClick={() => {
                    const next = new URLSearchParams(params);
                    next.set("cursor", data.next_cursor!);
                    next.set("to_ms", String(data.history_to_ms));
                    setParams(next);
                  }}
                >
                  下一页
                </ActionButton>
              ) : null}
            </div>
          </PageState.Stale>
        )}
      </section>
    </PageShell>
  );
}
