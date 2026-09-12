import { EmptyNote } from "@shared/ui/EmptyNote";

import type { NewsWalletFill, NewsWalletRosterMember } from "../../api/newsQueries";
import { displayTime } from "../../model/newsLabels";
import {
  walletDecimal,
  walletFillLabel,
  walletFillQuantity,
  walletTransactionUrl,
} from "../../model/walletFacts";

export function WalletRosterTable({ members }: { members: readonly NewsWalletRosterMember[] }) {
  return (
    <div className="news-wallets-scroll">
      <table className="news-wallets-table">
        <thead>
          <tr>
            <th>钱包</th>
            <th>来源表现榜</th>
            <th>规模榜</th>
            <th>来源平仓数 / 盈亏因子</th>
          </tr>
        </thead>
        <tbody>
          {members.map((member) => (
            <tr key={member.wallet}>
              <td>
                {member.handle || "未提供名称"}
                <code>{member.wallet}</code>
              </td>
              <td>{member.rank_quality ?? "未入榜"}</td>
              <td>{member.rank_whale ?? "未入榜"}</td>
              <td>
                {member.closed_trades} / {member.profit_factor ?? "未知"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function WalletFillsTable({ fills }: { fills: readonly NewsWalletFill[] }) {
  if (!fills.length) return <EmptyNote>当前页没有保留的交易流水。</EmptyNote>;
  return (
    <div className="news-wallets-scroll">
      <table className="news-wallets-table">
        <thead>
          <tr>
            <th>链时间 / 钱包</th>
            <th>动作 / 数量</th>
            <th>金额与来源</th>
            <th>链上证据</th>
          </tr>
        </thead>
        <tbody>
          {fills.map((fill) => {
            const url = walletTransactionUrl(fill);
            return (
              <tr key={`${fill.chain_id}:${fill.tx_hash}:${fill.log_index}`}>
                <td>
                  {displayTime(fill.event_at_ms)}
                  <code>{fill.wallet}</code>
                </td>
                <td>
                  {walletFillLabel(fill.kind)}
                  <small>{walletFillQuantity(fill)}</small>
                </td>
                <td>
                  {fill.usd === null ? "未计价" : `$${walletDecimal(fill.usd)}`}
                  <small>{fill.usd_source ?? "现金归属或计价不可用"}</small>
                </td>
                <td>
                  <small>
                    区块 {fill.block_number} / 日志 {fill.log_index}
                  </small>
                  {url ? (
                    <a href={url} rel="noopener noreferrer" target="_blank">
                      <code>{fill.tx_hash}</code>
                    </a>
                  ) : (
                    <code>{fill.tx_hash}</code>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
