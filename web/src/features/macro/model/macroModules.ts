import type { MacroAvailableModuleById, MacroModuleId } from "./macroTypes";

export type MacroModuleDefinition<ModuleId extends MacroModuleId = MacroModuleId> = {
  apiPath: `/api/macro/${string}`;
  id: ModuleId;
  label: string;
  routePath: `/macro/${string}`;
  routeSegment: string;
  schemaVersion: MacroAvailableModuleById[ModuleId]["schema_version"];
  sections: readonly { id: string; label: string }[];
};

type MacroModuleDefinitionMap = {
  [ModuleId in MacroModuleId]: MacroModuleDefinition<ModuleId>;
};

export const MACRO_MODULES = {
  rates_fed: {
    apiPath: "/api/macro/rates-fed",
    id: "rates_fed",
    label: "利率与美联储",
    routePath: "/macro/rates-fed",
    routeSegment: "rates-fed",
    schemaVersion: "macro_rates_fed_v8",
    sections: [
      { id: "curve", label: "收益率曲线" },
      { id: "policy", label: "政策走廊" },
      { id: "fed", label: "美联储沟通" },
      { id: "positioning", label: "利率仓位" },
    ],
  },
  economy_inflation: {
    apiPath: "/api/macro/economy-inflation",
    id: "economy_inflation",
    label: "经济与通胀",
    routePath: "/macro/economy-inflation",
    routeSegment: "economy-inflation",
    schemaVersion: "macro_economy_inflation_v6",
    sections: [
      { id: "inflation", label: "通胀" },
      { id: "labor", label: "就业" },
      { id: "growth", label: "增长" },
    ],
  },
  liquidity_funding: {
    apiPath: "/api/macro/liquidity-funding",
    id: "liquidity_funding",
    label: "流动性与融资",
    routePath: "/macro/liquidity-funding",
    routeSegment: "liquidity-funding",
    schemaVersion: "macro_liquidity_funding_v5",
    sections: [
      { id: "balance-sheet", label: "资产负债表" },
      { id: "funding", label: "融资条件" },
    ],
  },
  credit: {
    apiPath: "/api/macro/credit",
    id: "credit",
    label: "信用市场",
    routePath: "/macro/credit",
    routeSegment: "credit",
    schemaVersion: "macro_credit_v7",
    sections: [
      { id: "cycle", label: "周期四维" },
      { id: "spreads", label: "评级利差" },
      { id: "funding", label: "融资成本" },
      { id: "banks", label: "银行供需" },
      { id: "quality", label: "贷款质量" },
      { id: "confirmation", label: "市场确认" },
    ],
  },
  volatility: {
    apiPath: "/api/macro/volatility",
    id: "volatility",
    label: "波动率",
    routePath: "/macro/volatility",
    routeSegment: "volatility",
    schemaVersion: "macro_volatility_v7",
    sections: [
      { id: "term", label: "现货–3M" },
      { id: "cross-asset", label: "跨资产隐波" },
    ],
  },
  cross_asset: {
    apiPath: "/api/macro/cross-asset",
    id: "cross_asset",
    label: "大类资产与期货",
    routePath: "/macro/cross-asset",
    routeSegment: "cross-asset",
    schemaVersion: "macro_cross_asset_v8",
    sections: [
      { id: "returns", label: "收益矩阵" },
      { id: "normalized", label: "分组走势" },
      { id: "correlations", label: "相关矩阵" },
      { id: "futures", label: "期货与仓位" },
    ],
  },
} as const satisfies MacroModuleDefinitionMap;

export const MACRO_MODULE_DEFINITIONS = Object.values(MACRO_MODULES);
