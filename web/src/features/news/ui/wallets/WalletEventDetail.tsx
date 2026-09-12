import { ActionButton } from "@shared/ui/ActionButton";
import * as PageState from "@shared/ui/PageState";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useNewsWalletEventWithToken, type NewsWalletSnapshot } from "../../api/newsQueries";
import { displayTime, optionalTime } from "../../model/newsLabels";
import { walletDecimal, walletNotificationLabel, walletReason } from "../../model/walletFacts";

import { WalletFillsTable } from "./WalletSupportingTables";

export function WalletEventDetail({ token, episodeId }: { token: string; episodeId: string }) {
  return <EpisodeContent key={episodeId} token={token} episodeId={episodeId} />;
}

function EpisodeContent({ token, episodeId }: { token: string; episodeId: string }) {
  const [fillsCursor, setFillsCursor] = useState<string | undefined>();
  const [params, setParams] = useSearchParams();
  const query = useNewsWalletEventWithToken(token, episodeId, fillsCursor);
  const data = query.data;
  const close = () => {
    const next = new URLSearchParams(params);
    next.delete("episode");
    setParams(next);
  };
  if (query.isError && !data)
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  if (!data) return <PageState.Loading label="正在按事件标识读取详情" layout="panel" rows={6} />;
  const event = data.event;
  const first = event.initial_snapshot;
  const primary = first.fast.matched ? first.fast : first.slow;
  return (
    <section className="news-wallets-panel news-wallet-event-detail" aria-label="事件详情">
      <div className="news-wallets-toolbar">
        <h2>{event.token_symbol || event.token} · 集中净买入</h2>
        <ActionButton size="sm" onClick={close}>
          关闭详情
        </ActionButton>
      </div>
      <PageState.Stale
        failedRefresh={query.isError ? "详情刷新失败，保留已取得事实。" : undefined}
        updating={query.isFetching}
        onRetry={() => void query.refetch()}
      >
        <h3>发生了什么</h3>
        <p>
          首次触发 {displayTime(event.triggered_at_ms)} · {primary.window} · {primary.qualified_n}{" "}
          个合格地址 · 净买入 ${walletDecimal(primary.net_usd)}
        </p>
        <p>
          Robinhood Chain · <code>{event.token}</code>
        </p>
        <p>
          {event.ended_at_ms === null
            ? "本轮进行中"
            : `本轮已结束 · ${displayTime(event.ended_at_ms)}`}{" "}
          · {walletNotificationLabel(event.notification_state)}
          {event.notification_reason ? <> · {walletReason(event.notification_reason)}</> : null}
        </p>
        <p className="news-wallets-note">
          事件标识 <code>{event.episode_id}</code>
        </p>
        <h3>合格钱包与买卖净额 · 初始触发快照</h3>
        <Snapshot snapshot={first} />
        <p className="news-wallets-note">
          买入 − 卖出 = 窗口净支出；金额来自同一窗口、同一入选集合。 USDG 使用现金腿面值，不含独立
          gas 或未归属费用，不代表全部持仓、盈利或全市场净流入。 不同地址不等于独立主体。
        </p>
        <h3>原始交易时间线</h3>
        <WalletFillsTable fills={data.fills} />
        <div className="news-wallets-toolbar">
          {fillsCursor ? (
            <ActionButton size="sm" onClick={() => setFillsCursor(undefined)}>
              最新流水
            </ActionButton>
          ) : null}
          {data.next_fills_cursor ? (
            <ActionButton size="sm" onClick={() => setFillsCursor(data.next_fills_cursor!)}>
              更早流水
            </ActionButton>
          ) : null}
          <small>完整窗口统计独立于当前流水页</small>
        </div>
        <h3>当前变化与缺口</h3>
        <p>
          {walletReason(event.change_reason)} · 快照链时间{" "}
          {displayTime(event.latest_snapshot.cutoff_at_ms)}
        </p>
        <Snapshot snapshot={event.latest_snapshot} />
        <p className="news-wallets-note">
          窗口滑出不代表卖出，转出不代表清仓。最后有效增持
          {displayTime(event.last_effective_buy_at_ms)} 用于本轮去重，30 分钟无有效增持后结束。
        </p>
        <h3>事件后价格观察</h3>
        <p>
          {event.reference_price === null
            ? "未取得触发时的可靠价格基准，价格变化保持未知。"
            : `触发基准 $${walletDecimal(event.reference_price)} · ${optionalTime(event.reference_at_ms)} · ${event.reference_source}`}
        </p>
        <div className="news-wallets-outcomes">
          {(["15m", "1h", "4h"] as const).map((horizon) => {
            const receipt = data.outcomes.find((outcome) => outcome.horizon === horizon);
            return (
              <div key={horizon}>
                <b>{horizon}</b>
                <p>
                  {receipt
                    ? {
                        comparable: "可比较",
                        missing_reference: "缺少触发基准",
                        unavailable: "未取得价格",
                        late: "采样迟到，目标时点价格未知",
                      }[receipt.status]
                    : "尚无采样回执"}
                </p>
                {receipt ? (
                  <>
                    <p>目标 {displayTime(receipt.target_at_ms)}</p>
                    <p>实际 {displayTime(receipt.at_ms)}</p>
                    <p>
                      {receipt.price === null
                        ? "价格未知"
                        : `价格 $${walletDecimal(receipt.price)}`}
                    </p>
                    <p>
                      {receipt.change_percent === null
                        ? "变化未知"
                        : `观察后价格变化 ${walletDecimal(receipt.change_percent)}%`}
                    </p>
                  </>
                ) : null}
              </div>
            );
          })}
        </div>
        <details>
          <summary>检测与通知时间</summary>
          <p>
            链事件 {displayTime(event.triggered_at_ms)} → 收到 {displayTime(event.received_at_ms)}→
            检测 {displayTime(event.detected_at_ms)} → 意图 {optionalTime(event.intent_at_ms)}→
            首次尝试 {optionalTime(event.first_attempt_at_ms)}
          </p>
        </details>
      </PageState.Stale>
    </section>
  );
}

function Snapshot({ snapshot }: { snapshot: NewsWalletSnapshot }) {
  return (
    <div className="news-wallets-snapshots">
      {[snapshot.fast, snapshot.slow].map((window) => {
        const qualified = window.members.filter((member) => member.qualified);
        const others = window.members.filter((member) => !member.qualified);
        return (
          <section key={window.window}>
            <h4>
              {window.window} · {window.qualified_n} 个合格地址 ·{" "}
              {window.matched ? "满足条件" : "未满足条件"}
            </h4>
            <p>
              {displayTime(window.from_ms)} → {displayTime(window.to_ms)} · 至少 {window.required_n}{" "}
              个地址， 每地址净买入至少 ${walletDecimal(snapshot.min_net_buy_usd)}
            </p>
            <p>
              入选地址买入 ${walletDecimal(window.buy_usd)} · 卖出 ${walletDecimal(window.sell_usd)}{" "}
              · 净买入 ${walletDecimal(window.net_usd)}
            </p>
            <Members members={qualified} />
            <details>
              <summary>其他观察地址与未纳入原因 · {others.length}</summary>
              <Members members={others} />
            </details>
          </section>
        );
      })}
    </div>
  );
}

function Members({ members }: { members: NewsWalletSnapshot["fast"]["members"] }) {
  if (!members.length) return <p className="news-wallets-note">此集合没有地址。</p>;
  return (
    <div className="news-wallets-scroll">
      <table className="news-wallets-table">
        <thead>
          <tr>
            <th>地址 / 来源依据</th>
            <th>已计价买入</th>
            <th>已计价卖出</th>
            <th>净支出 / 净数量</th>
          </tr>
        </thead>
        <tbody>
          {members.map((member) => (
            <tr key={member.wallet}>
              <td>
                {member.handle || "未提供名称"}
                <code>{member.wallet}</code>
                <small>
                  名单版本 {member.roster_version ?? "未知"} · 来源表现榜{" "}
                  {member.rank_quality ?? "未入榜"}
                </small>
                <small>
                  来源平仓数 {member.source_closed_trades ?? "未知"} · 来源盈亏因子{" "}
                  {member.source_profit_factor ?? "未知"}
                </small>
                <small>资料取得 {optionalTime(member.roster_known_at_ms)}</small>
                <small>监控覆盖起点 {optionalTime(member.monitoring_from_ms)}</small>
                {member.reasons.map((reason) => (
                  <small key={reason}>{walletReason(reason)}</small>
                ))}
              </td>
              <td>${walletDecimal(member.buy_usd)}</td>
              <td>${walletDecimal(member.sell_usd)}</td>
              <td>
                {member.net_usd === null ? "口径不完整" : `$${walletDecimal(member.net_usd)}`}
                <small>{member.net_token_raw} raw</small>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
