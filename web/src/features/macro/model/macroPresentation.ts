import type {
  MacroCondition,
  MacroLiveDeltaV2,
  MacroModuleId,
  MacroOutcomeReplayV2,
  MacroThesisRunData,
  MacroThesisV2,
} from "./macroTypes";

export function formatInstant(value: number | null): string {
  if (!value) return "尚无时间";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    hour12: false,
    timeStyle: "short",
  }).format(new Date(value));
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

export function formatSigned(value: number | null): string {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${formatNumber(value)}`;
}

export function stanceLabel(value: MacroThesisV2["mainline"]["stance"]): string {
  return { call: "形成判断", no_call: "证据不足，暂不判断" }[value];
}

export function confidenceLabel(
  value: MacroThesisV2["mainline"]["confidence"] | null | undefined,
): string {
  if (!value) return "未声明置信度";
  return { high: "高置信度", low: "低置信度", medium: "中等置信度" }[value];
}

export function stageLabel(value: MacroThesisV2["mainline"]["stage"]): string {
  return {
    developing: "发展中",
    emerging: "形成中",
    mature: "成熟",
    reversing: "正在反转",
    uncertain: "尚不确定",
  }[value];
}

export function horizonLabel(value: string): string {
  return (
    {
      "1m": "1 个月",
      "1w": "1 周",
      "1w_to_1m": "1 周至 1 个月",
    }[value] ?? "期限未解释"
  );
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

export function moduleRoleLabel(
  value: MacroThesisV2["module_assessments"][number]["role"] | "not_material" | "unassessed",
): string {
  return {
    confirming: "确认",
    contradicting: "反证",
    driver: "驱动",
    not_material: "本次不重要",
    unassessed: "未评估",
    uncertain: "待确认",
  }[value];
}

export function changeStatusLabel(
  value: MacroThesisV2["material_changes"][number]["status"],
): string {
  return {
    new: "新增",
    reversed: "反转",
    strengthened: "增强",
    weakened: "减弱",
  }[value];
}

export function assetDirectionLabel(
  value: MacroThesisV2["asset_outlooks"][number]["direction"],
): string {
  return { bearish: "偏空", bullish: "偏多", neutral: "中性" }[value];
}

export function momentumLabel(value: MacroThesisV2["assets"][number]["momentum_1w"]): string {
  return { down: "下行", flat: "持平", insufficient: "动量证据不足", up: "上行" }[value];
}

export function conditionKindLabel(value: MacroCondition["kind"]): string {
  return {
    checkpoint: "事件检查点",
    confirmation: "确认条件",
    falsifier: "失效条件",
    weakening: "削弱条件",
  }[value];
}

export function operatorLabel(value: MacroCondition["operator"]): string {
  if (!value) return "事件";
  return { gt: ">", gte: "≥", lt: "<", lte: "≤" }[value];
}

export function liveDeltaLabel(value: MacroLiveDeltaV2["mainline_validity"]): string {
  return {
    confirming: "正在确认",
    insufficient: "新增证据不足",
    invalidation_triggered: "失效条件已触发",
    unrelated: "新增事实与主线无关",
    weakening: "正在削弱",
  }[value];
}

export function outcomeStatusLabel(
  value: MacroOutcomeReplayV2["horizons"][number]["status"],
): string {
  return { evaluated: "已评估", insufficient: "到期但证据不足", pending: "等待到期" }[value];
}

export function runStatusLabel(value: MacroThesisRunData["status"]): string {
  return (
    {
      config_error: "配置错误",
      failed: "失败",
      missing: "尚无运行",
      not_published: "未发布",
      pending: "等待运行",
      published: "已发布",
      retryable: "等待重试",
      running: "生成中",
    }[value] ?? "状态未知"
  );
}
