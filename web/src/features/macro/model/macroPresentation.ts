import { MACRO_MODULES } from "./macroModules";
import type { MacroModuleId } from "./macroTypes";

export const MACRO_CHART_COLORS = Array.from(
  { length: 10 },
  (_, index) => `var(--chart-series-${index + 1})`,
);

export function formatInstant(value: number | null): string {
  if (value == null) return "尚无时间";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    hour12: false,
    timeStyle: "short",
  }).format(new Date(value));
}

export function formatMacroNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

export function formatMacroSigned(value: number | null): string {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${formatMacroNumber(value)}`;
}

export function formatMacroUnit(unit?: string | null): string {
  if (!unit) return "";
  return (
    {
      basis_points: " bp",
      billions_chained_2017_usd: " 十亿 2017 年不变价美元",
      billions_usd: " 十亿美元",
      bp: " bp",
      index: " 点",
      index_points: " 点",
      millions_usd: " 百万美元",
      normalized_index: "（基期=100）",
      percent: "%",
      percent_open_interest: "% OI",
      persons: " 人",
      price: "（价格）",
      thousands_persons: " 千人",
      usd_per_barrel: " 美元/桶",
      usdt: " USDT",
    }[unit] ?? "（单位未解释）"
  );
}

export function formatMacroValue(value: number | null, unit?: string | null): string {
  return value == null ? "—" : `${formatMacroNumber(value)}${formatMacroUnit(unit)}`;
}

export function macroChartColor(index: number): string {
  return MACRO_CHART_COLORS[index % MACRO_CHART_COLORS.length]!;
}

export function moduleLabel(value: MacroModuleId): string {
  return MACRO_MODULES[value].label;
}
