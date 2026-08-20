import type { NewsEventReaction, NewsQuote, NewsReaction } from "../api/newsQueries";

/**
 * Price formatting for the two market values the console shows (#88).
 *
 * Nothing here computes a return, resolves an asset, ranks a venue or decides what is missing — those are
 * server answers. This module turns a server number into the characters on screen and picks a visual tone,
 * and that is all it is allowed to do.
 *
 * Up is red and down is green, the mainland market convention the direction chip already uses (#74). The two
 * axes are told apart by weight rather than hue: the model's judgment is a solid chip, the market's actual
 * move is plain figures.
 */
export type PriceTone = "up" | "down" | "flat";

/** A price arrives as an exact decimal string; only the display precision is the browser's business. */
export function formatPrice(value: string | null | undefined): string {
  if (!value) return "—";
  const price = Number(value);
  if (!Number.isFinite(price) || price <= 0) return "—";
  const digits = price >= 1000 ? 2 : price >= 1 ? 4 : 6;
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: price >= 1000 ? 2 : 0,
  }).format(price);
}

export function formatChangePct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

/** Returns are stored as integer basis points so the API and the aggregates stay exact; percent is display. */
export function formatBps(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const pct = value / 100;
  return `${pct > 0 ? "+" : ""}${pct.toFixed(2)}%`;
}

export function priceTone(value: number | null | undefined): PriceTone {
  if (value == null || !Number.isFinite(value) || value === 0) return "flat";
  return value > 0 ? "up" : "down";
}

/** A stale quote keeps its number and says so; it never disappears and never becomes zero. */
export function quoteAgeLabel(quote: NewsQuote): string {
  if (quote.age_ms == null) return quote.state_zh;
  const seconds = Math.round(quote.age_ms / 1000);
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes} 分钟前` : `${Math.round(minutes / 60)} 小时前`;
}

export function quoteVenueLabel(quote: NewsQuote): string {
  if (!quote.venue) return "";
  return quote.venue_symbol ? `${quote.venue}:${quote.venue_symbol}` : quote.venue;
}

/**
 * What a reaction cell says when it has no number. The horizon is fixed and historical, so "未到期" and
 * "无法计算" are different facts and neither is a zero return.
 */
export function reactionPlaceholder(
  reaction: NewsReaction | NewsEventReaction | null | undefined,
  horizon: "1h" | "4h",
): string {
  if (!reaction) return "—";
  if (reaction.state === "unavailable") return reaction.unavailable_reason_zh || "无法计算";
  if (horizon === "4h" && reaction.state === "partial") return "未到期";
  return reaction.state === "pending" ? "未到期" : "—";
}

export function reactionValue(
  reaction: NewsReaction | NewsEventReaction | null | undefined,
  horizon: "1h" | "4h",
): number | null {
  if (!reaction) return null;
  const value = horizon === "1h" ? reaction.return_1h_bps : reaction.return_4h_bps;
  return value ?? null;
}

/** The topbar figure: a hit rate is only shown with the denominator that earned it. */
export function hitFigure(pct: number | null | undefined, n: number): string {
  if (pct == null || !n) return "样本不足";
  return `${pct.toFixed(0)}% · N=${n}`;
}
