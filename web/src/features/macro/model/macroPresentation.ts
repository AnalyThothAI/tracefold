import type {
  MacroCondition,
  MacroLiveDeltaReadData,
  MacroModuleId,
  MacroOutcomeReplayReadData,
  MacroThesisRunData,
  MacroThesisV1,
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

export function stanceLabel(value: MacroThesisV1["mainline"]["stance"]): string {
  return { call: "形成判断", no_call: "证据不足，暂不判断" }[value];
}

export function confidenceLabel(value: MacroThesisV1["mainline"]["confidence"]): string {
  return { high: "高置信度", low: "低置信度", medium: "中等置信度" }[value];
}

export function stageLabel(value: MacroThesisV1["mainline"]["stage"]): string {
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
      "1d": "1 日",
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
  value: MacroThesisV1["module_assessments"][number]["role"],
): string {
  return {
    confirming: "确认",
    contradicting: "反证",
    driver: "驱动",
    uncertain: "待确认",
  }[value];
}

export function changeStatusLabel(
  value: MacroThesisV1["changes_from_prior"][number]["status"],
): string {
  return {
    new: "新增",
    reversed: "反转",
    strengthened: "增强",
    unchanged: "未变",
    weakened: "减弱",
  }[value];
}

export function leadingSideLabel(value: string, sideA: string, sideB: string): string {
  return (
    { balanced: "双方均衡", side_a: sideA, side_b: sideB, uncertain: "尚不确定" }[value] ??
    "尚不确定"
  );
}

export function assetDirectionLabel(
  value: MacroThesisV1["assets"][number]["outlook_1w"]["direction"],
): string {
  return {
    bearish: "偏空",
    bullish: "偏多",
    neutral: "中性",
    no_call: "证据不足，暂不判断",
  }[value];
}

export function momentumLabel(
  value: MacroThesisV1["assets"][number]["momentum"]["momentum_1w"],
): string {
  return { down: "下行", flat: "持平", insufficient: "动量证据不足", up: "上行" }[value];
}

export function conditionEffectLabel(value: MacroCondition["effect"]): string {
  return {
    confirming: "确认条件",
    invalidation_triggered: "失效条件",
    weakening: "削弱条件",
  }[value];
}

export function operatorLabel(value: MacroCondition["operator"]): string {
  return { abs_gte: "绝对值 ≥", gt: ">", gte: "≥", lt: "<", lte: "≤" }[value];
}

export function liveDeltaLabel(value: MacroLiveDeltaReadData["mainline_validity"]): string {
  return {
    confirming: "正在确认",
    insufficient: "新增证据不足",
    invalidation_triggered: "失效条件已触发",
    unrelated: "新增事实与主线无关",
    weakening: "正在削弱",
  }[value];
}

export function outcomeStatusLabel(
  value: MacroOutcomeReplayReadData["horizons"][number]["status"],
): string {
  return { evaluated: "已评估", insufficient: "到期但证据不足", pending: "等待到期" }[value];
}

export function gapAxisLabel(value: MacroThesisV1["gaps"][number]["axis"]): string {
  return {
    coverage: "覆盖缺口",
    current_health: "当前数据异常",
    history_depth: "历史深度不足",
  }[value];
}

export function runStatusLabel(value: MacroThesisRunData["status"]): string {
  return (
    {
      config_error: "配置错误",
      failed: "失败",
      pending: "等待运行",
      published: "已发布",
      retryable: "等待重试",
      running: "生成中",
    }[value] ?? "状态未知"
  );
}

export function runErrorLabel(value: string): string {
  return (
    {
      macro_thesis_configuration_error: "研究配置不可用",
      macro_thesis_generation_error: "研究生成失败",
      macro_thesis_review_error: "独立审阅失败",
    }[value] ?? "未分类发布错误"
  );
}
