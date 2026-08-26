import type {
  LeverageDecision,
  LeverageEvidenceStatus,
  LeveragePhase,
} from "../../model/leverageCases";

/**
 * The words and glyphs 杠杆异动 renders, in one place.
 *
 * Colour is not here on purpose: the CSS keys off `data-decision` / `data-phase` / `data-status`, so a tone
 * cannot be set at one call site and forgotten at another. 做多 is red and 做空 green — the mainland
 * convention the rest of the console reads in — and every pipeline-ish state stays on indigo/amber/grey.
 */
export const DECISION_LABEL: Record<LeverageDecision, { big: string; chip: string }> = {
  long: { big: "LONG · 做多", chip: "LONG" },
  no_trade: { big: "NO TRADE · 不交易", chip: "NO TRADE" },
  pending: { big: "评估中 · 未判", chip: "评估中" },
  short: { big: "SHORT · 做空", chip: "SHORT" },
};

export const PHASE_LABEL: Record<LeveragePhase, string> = {
  active: "进行中",
  forming: "酝酿中",
  no_trade: "已判定",
  resolved: "已了结",
};

export const EVIDENCE_LABEL: Record<LeverageEvidenceStatus, string> = {
  conflict: "冲突",
  missing: "缺失",
  na: "不适用",
  support: "支持",
};

/** A glyph per state, so the card can preview the matrix without repeating four words on every row. */
export const EVIDENCE_GLYPH: Record<LeverageEvidenceStatus, string> = {
  conflict: "×",
  missing: "—",
  na: "·",
  support: "✓",
};

/*
 * Frozen, then live, then settled — the order the case actually moves through, and the order a reader has
 * to read them in if the frozen figures are to anchor the live ones rather than the other way round.
 * `Object.keys` walks insertion order, so this literal *is* the tab order.
 */
export const PLANE_LABEL: Record<"t0" | "now" | "out", { label: string; note: string }> = {
  t0: { label: "触发时", note: "冻结 · cutoff 之前可见的事实" },
  now: { label: "现在", note: "现在 · 独立报价轮询，从不回写案例" },
  out: { label: "结果", note: "结果 · 到期即冻结，不与现价混排" },
};
