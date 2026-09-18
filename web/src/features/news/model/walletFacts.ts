import {
  NEWS_WALLET_HISTORY_RANGES,
  type NewsWalletEventFilters,
  type NewsWalletFill,
  type NewsWalletFillKind,
  type NewsWallets,
  type NewsWalletSnapshot,
} from "../api/newsQueries";

import { formatPrice } from "./newsPrice";

/** Signed ledger values and tiny observations must never become a missing price or a fabricated zero. */
export function walletDecimal(value: string | null | undefined): string {
  if (value == null || value === "") return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount) || Math.abs(amount) < 0.000001) return value;
  return amount < 0 ? "-" + formatPrice(value.slice(1)) : formatPrice(value);
}

/** Under an hour on chain at the trigger, a token is labelled a new listing — never filtered out. */
export const WALLET_NEW_LAUNCH_MAX_AGE_MS = 3_600_000;

/**
 * How old the token was when the episode triggered, from the tape's own earliest sighting of it.
 * That sighting is a bound and not the chain's first block — the collector only ever watches roster
 * addresses — so the label says where the number comes from, and an unseen token stays unknown.
 */
export function walletTokenAge(snapshot: NewsWalletSnapshot): string {
  const seen = snapshot.token_first_seen_at_ms;
  if (seen == null) return "代币年龄未知";
  const age = Math.max(0, snapshot.cutoff_at_ms - seen);
  const minutes = Math.floor(age / 60_000);
  const text =
    minutes < 60
      ? `${minutes} 分钟`
      : minutes < 1440
        ? `${Math.floor(minutes / 60)} 小时`
        : `${Math.floor(minutes / 1440)} 天`;
  return age < WALLET_NEW_LAUNCH_MAX_AGE_MS ? `代币年龄 ${text} · 新盘` : `代币年龄 ${text}`;
}

/**
 * How often this window's qualified addresses have qualified before, over the server's own window.
 * Participations, not episodes: two of these addresses in one earlier round is two participations,
 * and a snapshot written before the count existed says it is unknown rather than printing a zero.
 */
export function walletParticipations(window: NewsWalletSnapshot["window"]): string {
  const counts = window.members
    .filter((member) => member.qualified)
    .map((member) => member.recent_episodes);
  const known = counts.filter((count): count is number => count != null);
  if (!known.length) return "近 14 天参与次数未知";
  return `近 14 天共参与 ${known.reduce((total, count) => total + count, 0)} 次`;
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

/**
 * Why this page currently shows what it shows. Every branch is a different answer to "there is no
 * alert": a list smaller than the quorum, addresses that have not watched long enough, a collection
 * that is behind, a real absence of qualifying buys, and the three ways the page itself or the send
 * chain can be the reason. The browser decides none of the numbers — it orders the answers.
 */
export type WalletStatusState =
  | "unread"
  | "query_failed"
  | "notifications_disabled"
  | "roster_insufficient"
  | "warming_up"
  | "collection_lagging"
  | "send_failed"
  | "no_match"
  | "healthy";

export function walletStatusState(
  status: NewsWallets | undefined,
  { failed }: { failed: boolean },
): WalletStatusState {
  // An unanswered read is never an answer: "nothing has come back yet" is its own state, not health.
  if (!status) return failed ? "query_failed" : "unread";
  if (!status.notifications_enabled) return "notifications_disabled";
  const { required_n, sufficient } = status.thresholds;
  if (!sufficient)
    return status.roster.address_count < required_n ? "roster_insufficient" : "warming_up";
  if (status.collection_lagging) return "collection_lagging";
  if (status.funnel.intents > status.funnel.sent) return "send_failed";
  return status.funnel.events === 0 ? "no_match" : "healthy";
}

/** One sentence naming the state, built from the server's own counts and thresholds. */
export function walletStatusSentence(
  state: WalletStatusState,
  status: NewsWallets | undefined,
): string {
  if (state === "unread") return "正在读取名单与采集状态。";
  if (!status || state === "query_failed") return "名单与采集状态读取失败，事件仍可查阅。";
  const { roster, thresholds, funnel } = status;
  const quorum = `30 分钟 ${thresholds.required_n} 个地址的门槛`;
  switch (state) {
    case "notifications_disabled":
      return "钱包通知已关闭：仍在采集与记录事件，不会发送任何通知。";
    case "roster_insufficient":
      return `当前名单地址 ${roster.address_count} 个，低于 ${quorum}；当前名单不足以触发`;
    case "warming_up":
      return `名单地址 ${roster.address_count} 个，其中 ${roster.supported_count} 个已具备完整窗口监控支持；其余仍在预热，尚不足以凑齐 ${quorum}。`;
    case "collection_lagging":
      return "链采集落后于当前时间，窗口内的变化可能尚未完整。";
    case "send_failed":
      return `${funnel.intents - funnel.sent} 个首报意图没有送达：${walletReason(funnel.unsent_reason)}。`;
    case "no_match":
      return "名单与采集正常，24 小时内没有满足条件的集中净买入。";
    default:
      return `名单与采集正常，24 小时内 ${funnel.events} 轮事件、${funnel.sent} 条已发送。`;
  }
}

export function walletReason(reason: string | null | undefined): string {
  if (!reason) return "未发送";
  const labels: Record<string, string> = {
    not_on_roster: "不在已发布名单内",
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
    episode_already_reported: "本轮已首报，不重复发送",
    merging_into_prepared_card: "正在并入待发送卡片",
    market_sender_unavailable: "发送渠道暂不可用",
    before_cutover: "产品切换前的事实，不补发",
    conditions_not_met: "未达到集中净买入条件",
    active_episode: "本轮已在进行中",
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
