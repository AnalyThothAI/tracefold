import {
  NEWS_WALLET_CARD_WINDOWS,
  type NewsWalletCard,
  type NewsWalletCardFilters,
  type NewsWalletCardKind,
  type NewsWalletCardWindow,
  type NewsWalletFillKind,
  type NewsWalletFill,
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
  buy: "买入",
  exit: "减仓",
  crowding: "拥挤",
  digest: "摘要",
};

const WALLET_CARD_TITLES: Record<NewsWalletCardKind, string> = {
  buy: "买入观察：单钱包的买入候选，保留阶段、依据与后续价格",
  exit: "退出卡：名单地址卖出超过阈值比例，或卖掉最后一笔",
  crowding: "拥挤卡：多个名单地址在同一窗口内首次买入同一代币",
  digest: "摘要：程序计算并渲染事实，模型选择买入研究材料",
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

export function walletBasisLabel(basis: string | null | undefined): string {
  if (!basis) return "—";
  return WALLET_BASIS_LABELS[basis] ?? basis;
}

/** `?window=` as the server's own closed vocabulary; anything else is the default rather than a 4xx. */
export function parseWalletWindow(value: string | null): NewsWalletCardWindow {
  const found = NEWS_WALLET_CARD_WINDOWS.find((window) => window === value);
  return found ?? "24h";
}

export const WALLET_CARD_FILTERS = ["buy", "all", "crowding", "exit", "digest"] as const;

export function parseWalletFilters(params: URLSearchParams): NewsWalletCardFilters {
  const kind = WALLET_CARD_FILTERS.find((value) => value === params.get("kind")) ?? "buy";
  const address = (key: string) => {
    const value = params.get(key)?.trim() ?? "";
    return /^0x[0-9a-f]{40}$/i.test(value) ? value.toLowerCase() : "";
  };
  return {
    window: parseWalletWindow(params.get("window")),
    kind,
    walletAddress: address("wallet_address"),
    tokenAddress: address("token_address"),
  };
}

export function nextWalletParams(filters: NewsWalletCardFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.window !== "24h") params.set("window", filters.window);
  if (filters.kind !== "buy") params.set("kind", filters.kind);
  if (filters.walletAddress) params.set("wallet_address", filters.walletAddress);
  if (filters.tokenAddress) params.set("token_address", filters.tokenAddress);
  return params;
}

export function walletHistoryPath(filters: NewsWalletCardFilters): string {
  return `/news/wallets?${nextWalletParams({ ...filters, kind: "all" })}`;
}

const WALLET_STAGE_LABELS: Record<NonNullable<NewsWalletCard["stage"]>, string> = {
  first_observed: "观察期首次买入",
  new_position: "已核实新仓",
  add: "加仓",
  reentry: "清仓后重入",
  unknown: "阶段未知",
};

export function walletStageLabel(stage: NewsWalletCard["stage"]): string {
  return stage == null ? "阶段未知" : WALLET_STAGE_LABELS[stage];
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
  if (card.kind === "buy") return walletStageLabel(card.stage);
  if (card.kind === "exit") {
    if (card.closed) return "清仓";
    return card.ratio_bps == null ? "—" : `${(card.ratio_bps / 100).toFixed(0)}%`;
  }
  if (card.kind === "crowding") return `${card.peer_wallets} 个地址`;
  return card.digest_model_used ? "模型选材" : "程序选材";
}
