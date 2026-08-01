import type { MacroModuleId } from "./macroTypes";

export function formatInstant(value: number | null): string {
  if (!value) return "尚无时间";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    hour12: false,
    timeStyle: "short",
  }).format(new Date(value));
}

export function moduleLabel(value: MacroModuleId): string {
  return {
    credit: "信用市场",
    cross_asset: "大类资产与期货",
    economy_inflation: "经济与通胀",
    liquidity_funding: "流动性与融资",
    rates_fed: "利率与美联储",
    volatility: "波动率",
  }[value];
}
