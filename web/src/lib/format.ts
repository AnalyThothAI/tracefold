export function compactNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) {
    return `${trim(value / 1_000_000_000)}B`;
  }
  if (abs >= 1_000_000) {
    return `${trim(value / 1_000_000)}M`;
  }
  if (abs >= 1_000) {
    return `${trim(value / 1_000)}K`;
  }
  return String(Math.round(value));
}

export function formatRelativeTime(value: number | null | undefined, now = Date.now()): string {
  if (!value) {
    return "-";
  }
  const delta = Math.max(0, now - value);
  if (delta < 60_000) {
    return `${Math.floor(delta / 1000)}s`;
  }
  if (delta < 3_600_000) {
    return `${Math.floor(delta / 60_000)}m`;
  }
  if (delta < 86_400_000) {
    return `${Math.floor(delta / 3_600_000)}h`;
  }
  return `${Math.floor(delta / 86_400_000)}d`;
}

export function formatPercentShare(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  const percent = Math.max(0, value) * 100;
  return percent >= 10 ? `${Math.round(percent)}%` : `${percent.toFixed(1).replace(/\.0$/, "")}%`;
}

export function formatSignedPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  const percent = Math.abs(value) * 100;
  const formatted =
    percent >= 10 ? `${Math.round(percent)}%` : `${percent.toFixed(1).replace(/\.0$/, "")}%`;
  return `${value > 0 ? "+" : value < 0 ? "-" : ""}${formatted}`;
}

export function formatUsdCompact(value: number | null | undefined): string {
  const compact = compactNumber(value);
  return compact === "-" ? "-" : `$${compact}`;
}

export function formatTokenPriceUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);
  if (abs === 0) {
    return "$0";
  }
  if (abs >= 1_000) {
    return `${sign}$${compactNumber(abs)}`;
  }
  if (abs >= 1) {
    return `${sign}$${abs.toFixed(2)}`;
  }
  if (abs >= 0.01) {
    return `${sign}$${trimFixed(abs, 4)}`;
  }
  if (abs >= 0.000001) {
    return `${sign}$${trimFixed(abs, 8)}`;
  }
  return `${sign}$${abs.toExponential(2)}`;
}

export function shortAddress(value?: string | null): string {
  if (!value) {
    return "-";
  }
  return value.length > 18 ? `${value.slice(0, 8)}...${value.slice(-6)}` : value;
}

export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return String(Math.round(value));
}

export function formatRisk(value: string | null | undefined): string {
  const labels: Record<string, string> = {
    author_concentration_high: "作者集中",
    duplicate_text_cluster: "重复文本",
    repeated_text_cluster: "重复文本簇",
    market_missing: "市场缺失",
    missing_market: "市场缺失",
    no_venue: "无交易场所",
    public_stream_coverage: "公共流覆盖",
    thin_author_set: "作者过少",
    thin_mentions: "提及过少",
    insufficient_baseline: "基线不足",
    insufficient_history: "历史不足",
    pending_observation: "市场观测中",
    market_observation_pending: "市场观测中",
    provider_not_configured: "行情源未配置",
    provider_not_found: "行情源无结果",
    provider_error: "行情源错误",
    rate_limited: "行情限流",
    dead: "行情观测失败",
    stale_market: "市场过旧",
    identity_not_tradeable: "身份不可交易",
    low_information_posts: "信息密度低",
    attribution_confidence_low: "归因置信低",
    missing_social_start: "社交起点缺失",
    chase_risk: "追高风险",
    missing_price: "价格缺失",
    missing_social_history: "社交历史不足",
    market_freshness_missing: "市场新鲜度不足",
    market_metadata_missing: "市场元数据不足",
    SYMBOL_NOT_IN_REGISTRY: "registry 未命中",
    SYMBOL_CANDIDATES_STALE: "候选价格过期",
    ADDRESS_NOT_IN_REGISTRY: "地址未入库",
    NO_MARKET_DOMINANT_CHAIN_ASSET: "候选不唯一",
    CEX_PRICEFEED_NOT_IN_REGISTRY: "CEX 行情未入库",
  };
  return labels[value ?? ""] ?? (value ? value.replaceAll("_", " ") : "-");
}

function trim(value: number): string {
  return value.toFixed(value >= 10 ? 0 : 1).replace(/\.0$/, "");
}

function trimFixed(value: number, digits: number): string {
  return value
    .toFixed(digits)
    .replace(/(\.\d*?[1-9])0+$/, "$1")
    .replace(/\.0+$/, "");
}
