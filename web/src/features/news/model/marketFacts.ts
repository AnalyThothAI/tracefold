import {
  NEWS_MARKET_KINDS,
  type NewsMarketKind,
  type NewsMarketObservation,
} from "../api/newsQueries";

/**
 * Display helpers for the market observations `/api/news/market` serves (#553 PR-1).
 *
 * Nothing here judges and nothing here parses. `market_kind` and `parse_status` are closed server
 * vocabularies and get a Chinese word each; `notification_status` and `notification_reason` are open
 * strings the notification owner writes, so they are rendered verbatim — a lookup table here would either
 * silently drop a status it had never seen or rename one the operator greps for.
 */

const MARKET_KIND_LABELS: Record<NewsMarketKind, string> = {
  oi: "OI",
  liquidation: "清算",
  smart_money: "聪明钱",
  unknown_market: "原文",
};

const MARKET_KIND_TITLES: Record<NewsMarketKind, string> = {
  oi: "持仓异动：供应商在自己的触发条件下发一帧",
  liquidation: "强平：单笔强制平仓回报",
  smart_money: "聪明钱：被跟踪账户的开平仓动作",
  unknown_market: "未识别来源：没有解析器，只保留供应商原始行",
};

export function marketKindLabel(kind: NewsMarketKind): string {
  return MARKET_KIND_LABELS[kind];
}

export function marketKindTitle(kind: NewsMarketKind): string {
  return MARKET_KIND_TITLES[kind];
}

/** `parsed` means the parser read fields out of the record; `raw` means only the provider's line was kept. */
export function marketParseLabel(status: NewsMarketObservation["parse_status"]): string {
  return status === "parsed" ? "已解析" : "仅原文";
}

/**
 * `?kind=` as the server's own comma-separated subset.
 *
 * An unknown word is dropped rather than 4xx'd, the way the feed's filters are, and a value that narrows
 * nothing — empty, or all four — is the absence of the filter.
 */
export function parseMarketKinds(value: string | null): NewsMarketKind[] {
  if (!value) return [];
  const selected = new Set(value.split(","));
  const kinds = NEWS_MARKET_KINDS.filter((kind) => selected.has(kind));
  return kinds.length === NEWS_MARKET_KINDS.length ? [] : kinds;
}

export function toggleMarketKind(
  selected: readonly NewsMarketKind[],
  kind: NewsMarketKind,
): NewsMarketKind[] {
  const next = new Set(selected);
  if (next.has(kind)) next.delete(kind);
  else next.add(kind);
  const kinds = NEWS_MARKET_KINDS.filter((candidate) => next.has(candidate));
  return kinds.length === NEWS_MARKET_KINDS.length ? [] : kinds;
}

export function nextMarketParams(kinds: readonly NewsMarketKind[]): URLSearchParams {
  const params = new URLSearchParams();
  if (kinds.length) params.set("kind", kinds.join(","));
  return params;
}

/**
 * What the row is about, in the observation's own words.
 *
 * `symbol` is the parser's normalized subject and `raw_instrument` is what the provider wrote; a record
 * that carries neither says nothing rather than having a subject guessed out of its title.
 */
export function marketSubject(observation: NewsMarketObservation): string {
  return observation.symbol || observation.raw_instrument || "—";
}

/**
 * The observation's own stored fields, as the parser wrote them.
 *
 * Only what is present: a `null` is a field this record does not carry, and printing `—` for twenty of them
 * would bury the three that matter. Nothing is derived — every value here is one stored column.
 */
export function marketObservationTrace(
  observation: NewsMarketObservation,
): Array<[string, string]> {
  const fields: Array<[string, unknown]> = [
    ["provider", observation.provider],
    ["source_venue", observation.source_venue],
    ["ingest_mode", observation.ingest_mode],
    ["symbol", observation.symbol],
    ["raw_instrument", observation.raw_instrument],
    ["direction", observation.direction],
    ["action", observation.action],
    ["position_side", observation.position_side],
    ["liquidated_position_side", observation.liquidated_position_side],
    ["forced_order_side", observation.forced_order_side],
    ["price", observation.price],
    ["notional_usd", observation.notional_usd],
    ["pnl_usd", observation.pnl_usd],
    ["oi_change_bps", observation.oi_change_bps],
    ["oi_value_usd", observation.oi_value_usd],
    ["whale_oi_ratio_bps", observation.whale_oi_ratio_bps],
    ["whale_long_profit_bps", observation.whale_long_profit_bps],
    ["measurement_definition", observation.measurement_definition],
    ["account_address", observation.account_address],
    ["trader_label", observation.trader_label],
    ["source_strategy_id", observation.source_strategy_id],
    ["historical", observation.historical || null],
  ];
  return fields
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => [key, String(value)]);
}
