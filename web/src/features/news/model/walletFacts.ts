import {
  NEWS_WALLET_CARD_WINDOWS,
  type NewsWalletCard,
  type NewsWalletCardKind,
  type NewsWalletCardWindow,
  type NewsWalletFillKind,
} from "../api/newsQueries";

/**
 * Display helpers for the chain wallet tape (#572 PR-3).
 *
 * The same rule the market page follows: a closed server vocabulary — the card kind, the fill kind, the
 * verification basis, the window — gets one Chinese word from a `Record`, and an open server string —
 * a delivery state, a receipt source — is printed verbatim. A lookup table over an open string would
 * either drop a value it had never seen or rename one an operator greps for.
 *
 * Nothing here computes. The three cost bases, the exit ratio and the +1h/+4h returns are all server
 * answers; this module turns them into characters.
 */

const WALLET_CARD_LABELS: Record<NewsWalletCardKind, string> = {
  exit: "减仓",
  crowding: "拥挤",
  digest: "摘要",
};

const WALLET_CARD_TITLES: Record<NewsWalletCardKind, string> = {
  exit: "退出卡：名单地址卖出超过阈值比例，或卖掉最后一笔",
  crowding: "拥挤卡：多个名单地址在同一窗口内首次买入同一代币",
  digest: "摘要：每四小时一次，数字由程序算出，模型只写句子",
};

const WALLET_FILL_LABELS: Record<NewsWalletFillKind, string> = {
  buy: "买入",
  sell: "卖出",
  transfer_out: "转出",
};

/**
 * Where an exit ratio's denominator came from. Not a confidence score and not a warning: `链上余额` is
 * `balanceOf` at the block before the sell, `持仓推算` is the provider's reported bag plus the amount
 * that just left. A reader is owed the difference and nothing more alarming than the difference.
 */
const WALLET_BASIS_LABELS: Record<string, string> = {
  chain_balance: "链上余额",
  site_reported: "持仓推算",
};

export function walletCardLabel(kind: NewsWalletCardKind): string {
  return WALLET_CARD_LABELS[kind];
}

export function walletCardTitle(kind: NewsWalletCardKind): string {
  return WALLET_CARD_TITLES[kind];
}

export function walletFillLabel(kind: NewsWalletFillKind): string {
  return WALLET_FILL_LABELS[kind];
}

export function walletBasisLabel(basis: string | null | undefined): string {
  if (!basis) return "—";
  return WALLET_BASIS_LABELS[basis] ?? basis;
}

/** `?window=` as the server's own closed vocabulary; anything else is the default rather than a 4xx. */
export function parseWalletWindow(value: string | null): NewsWalletCardWindow {
  const found = NEWS_WALLET_CARD_WINDOWS.find((window) => window === value);
  return found ?? "24h";
}

export function nextWalletParams(window: NewsWalletCardWindow): URLSearchParams {
  const params = new URLSearchParams();
  if (window !== "24h") params.set("window", window);
  return params;
}

/**
 * What a card is about, in one cell. A digest is about a window rather than a subject, so it says so
 * instead of printing an empty token: the row's own lines carry the content.
 */
export function walletCardSubject(card: NewsWalletCard): string {
  if (card.kind === "digest") return "全名单";
  return card.token_symbol || card.token.slice(0, 10) || "—";
}

/** The one figure that differs per kind: an exit's share of the position, a crowd's headcount. */
export function walletCardMeasure(card: NewsWalletCard): string {
  if (card.kind === "exit") {
    if (card.closed) return "清仓";
    return card.ratio_bps == null ? "—" : `${(card.ratio_bps / 100).toFixed(0)}%`;
  }
  if (card.kind === "crowding") return `${card.peer_wallets} 个地址`;
  return card.digest_model_used ? "模型措辞" : "模板措辞";
}
