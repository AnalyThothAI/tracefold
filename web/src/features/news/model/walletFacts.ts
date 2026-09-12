import {
  NEWS_WALLET_HISTORY_RANGES,
  type NewsWalletEventFilters,
  type NewsWalletFill,
  type NewsWalletFillKind,
} from "../api/newsQueries";

import { formatPrice } from "./newsPrice";

/** Signed ledger values and tiny observations must never become a missing price or a fabricated zero. */
export function walletDecimal(value: string | null | undefined): string {
  if (value == null || value === "") return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount) || Math.abs(amount) < 0.000001) return value;
  return amount < 0 ? "-" + formatPrice(value.slice(1)) : formatPrice(value);
}

export function walletFillLabel(kind: NewsWalletFillKind): string {
  return { buy: "买入", sell: "卖出", transfer_out: "转出" }[kind];
}

/** Insert the token's decimal point without rounding an on-chain integer through a JS number. */
export function walletFillQuantity(
  fill: Pick<NewsWalletFill, "amount_raw" | "token_decimals">,
): string {
  const raw = fill.amount_raw;
  const decimals = fill.token_decimals;
  if (
    decimals == null ||
    !/^\d+$/.test(raw) ||
    !Number.isInteger(decimals) ||
    decimals < 0 ||
    decimals > 255
  )
    return `${raw} raw（精度未知）`;
  const digits = raw.replace(/^0+(?=\d)/, "").padStart(decimals + 1, "0");
  if (decimals === 0) return digits;
  const fractional = digits.slice(-decimals).replace(/0+$/, "");
  return `${digits.slice(0, -decimals)}${fractional ? `.${fractional}` : ""}`;
}

/** The official mainnet explorer; provider text never supplies the link's origin or path. */
export function walletTransactionUrl(
  fill: Pick<NewsWalletFill, "chain_id" | "tx_hash">,
): string | null {
  return fill.chain_id === 4663 && /^0x[0-9a-fA-F]{64}$/.test(fill.tx_hash)
    ? `https://robinhoodchain.blockscout.com/tx/${fill.tx_hash}`
    : null;
}

export function parseWalletEventFilters(params: URLSearchParams): NewsWalletEventFilters {
  return {
    historyRange:
      NEWS_WALLET_HISTORY_RANGES.find((value) => value === params.get("history_range")) ?? "24h",
    ...(params.get("cursor") ? { cursor: params.get("cursor")! } : {}),
    ...(params.get("to_ms") ? { toMs: Number(params.get("to_ms")) } : {}),
  };
}

export function walletReason(reason: string | null | undefined): string {
  if (!reason) return "未发送";
  const labels: Record<string, string> = {
    not_quality_roster: "不属于来源表现榜",
    incomplete_monitoring_window: "监控尚未覆盖完整窗口",
    collection_gap: "采集覆盖存在缺口",
    unpriced_trade: "存在未计价买卖",
    transfer_out_incomplete: "存在转出，净买入口径不完整",
    below_min_net_buy: "净买入未达门槛",
    nonpositive_net_quantity: "净买入数量不为正",
    invalidated_before_send: "发送前条件已失效",
    stale_before_send: "发送前已超时",
    stale_trigger: "触发事实已超时",
    future_chain_timestamp: "链时间超前",
    wallet_notifications_disabled: "钱包通知已静音",
    wallet_net_buy_cutover: "产品切换，旧意图已终结",
    roster_changed: "名单发生变化",
    window_expiry: "成交滑出观察窗口",
    inactivity_window: "30 分钟无有效增持，本轮结束",
    sell: "新增卖出改变净额",
    buy: "新增买入改变净额",
    triggered: "首次达到集中净买入条件",
  };
  return labels[reason] ?? reason;
}

export function walletNotificationLabel(state: string): string {
  return (
    (
      {
        sent: "已发送",
        failed: "未发送",
        unknown: "发送结果未知",
        sending: "发送中",
        pending: "等待发送",
        unavailable: "发送暂不可用",
        not_alerted: "未发送",
      } as Record<string, string>
    )[state] ?? state
  );
}
