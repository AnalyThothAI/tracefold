import { EmptyNote } from "@shared/ui/EmptyNote";
import { Link } from "react-router-dom";

import type {
  NewsWalletCardFilters,
  NewsWalletFill,
  NewsWalletRosterMember,
} from "../../api/newsQueries";
import { clockTime, displayTime, formatCount } from "../../model/newsLabels";
import { formatPrice, priceTone } from "../../model/newsPrice";
import {
  walletHistoryPath,
  walletFillLabel,
  walletFillQuantity,
  walletTransactionUrl,
} from "../../model/walletFacts";

export function WalletRosterTable({
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
            <th scope="col">钱包</th>
            <th scope="col">粉丝</th>
            <th scope="col">来源表现榜</th>
            <th scope="col">大户榜</th>
            <th scope="col">已实现盈亏</th>
            <th scope="col">盈亏因子</th>
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

export function WalletFillsTable({
  fills,
  limit,
}: {
  fills: readonly NewsWalletFill[];
  limit: number;
}) {
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
