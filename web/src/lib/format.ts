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

function trim(value: number): string {
  return value.toFixed(value >= 10 ? 0 : 1).replace(/\.0$/, "");
}

function trimFixed(value: number, digits: number): string {
  return value
    .toFixed(digits)
    .replace(/(\.\d*?[1-9])0+$/, "$1")
    .replace(/\.0+$/, "");
}
