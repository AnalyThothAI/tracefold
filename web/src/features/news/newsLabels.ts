import { formatNewsLocalTimestamp } from "./newsTime";

const ADMISSION_LABELS: Record<string, string> = {
  candidate: "候选",
  listing_deterministic: "上币（确定性）",
  recovery: "补录",
  suppressed_low_signal: "抑制 · 低信号",
  suppressed_pr_template: "抑制 · 律所模板",
};

const PRIORITY_LABELS: Record<string, string> = {
  high: "高优先",
  normal: "普通",
};

const DECISION_LABELS: Record<string, string> = {
  degraded: "降级",
  drop: "丢弃",
  escalate: "升级",
  push: "推送",
  throttled: "节流",
};

const DIRECTION_LABELS: Record<string, string> = {
  bearish: "看空",
  bullish: "看多",
  neutral: "中性",
  unclear: "不明",
};

const ASSET_CLASS_LABELS: Record<string, string> = {
  crypto: "加密",
  equity_or_commodity: "股票/商品",
  macro: "宏观",
  none: "无资产",
};

const FAMILY_LABELS: Record<string, string> = {
  disaster: "灾害",
  filing: "文件/公告",
  general: "综合",
  market_telemetry: "市场遥测",
};

const DELIVERY_STATE_LABELS: Record<string, string> = {
  sending: "发送中",
  sent: "已发送",
  suppressed: "已抑制",
  terminal: "已终结",
};

const STAGE_LABELS: Record<string, string> = {
  deep: "Analyst",
  triage: "Triage",
};

const SCOPE_LABELS: Record<string, string> = {
  macro: "宏观",
  sector: "板块",
  single_name: "单一标的",
};

export function admissionLabel(value: string): string {
  return ADMISSION_LABELS[value] ?? value;
}

export function priorityLabel(value: string): string {
  return PRIORITY_LABELS[value] ?? value;
}

export function decisionLabel(value: string): string {
  return DECISION_LABELS[value] ?? value;
}

export function directionLabel(value: string): string {
  return DIRECTION_LABELS[value] ?? value;
}

export function assetClassLabel(value: string): string {
  return ASSET_CLASS_LABELS[value] ?? value;
}

export function familyLabel(value: string): string {
  return FAMILY_LABELS[value] ?? value;
}

export function deliveryStateLabel(value: string): string {
  return DELIVERY_STATE_LABELS[value] ?? value;
}

export function stageLabel(value: string): string {
  return STAGE_LABELS[value] ?? value;
}

export function scopeLabel(value: string): string {
  return SCOPE_LABELS[value] ?? value;
}

export function magnitudeLabel(value: number | null | undefined): string | null {
  return typeof value === "number" && Number.isFinite(value) ? `M${value}` : null;
}

export function relativeTime(value: number): string {
  const minutes = Math.max(0, Math.floor((Date.now() - value) / 60_000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} 小时前` : `${Math.floor(hours / 24)} 天前`;
}

export function absoluteTime(value: number): string {
  return formatNewsLocalTimestamp(value);
}

export function displayTime(value: number): string {
  return `${absoluteTime(value)} · ${relativeTime(value)}`;
}

export function optionalTime(value: number | null | undefined): string {
  return value == null ? "尚无" : absoluteTime(value);
}

export function optionalDuration(value: number | null | undefined): string {
  if (value == null) return "尚无样本";
  return value < 1_000 ? `${value} ms` : `${(value / 1_000).toFixed(1)} s`;
}

export function validExternalUrl(value: string | null | undefined): string | null {
  const normalized = value?.trim() ?? "";
  return /^https?:\/\//i.test(normalized) ? normalized : null;
}

export function formatPoints(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}
