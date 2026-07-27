import type { components } from "@lib/types";

export type NewsStorySummary = components["schemas"]["NewsStorySummaryData"];
export type NewsStoryDetail = components["schemas"]["NewsStoryDetailData"];
export type NewsGlobalBrief = components["schemas"]["NewsGlobalBriefData"];
export type NewsBriefPublication = components["schemas"]["NewsBriefPublicationData"];
export type NewsEvidencePosture = NewsStorySummary["evidence_posture"];
export type NewsLifecycle = NewsStorySummary["lifecycle"];

export const evidencePostureLabel = (status: NewsEvidencePosture | string): string => {
  if (status === "independently_corroborated") return "独立多源佐证";
  if (status === "primary_source_confirmed") return "一手材料确认";
  if (status === "contested") return "证据有争议";
  if (status === "corrected") return "已更正";
  if (status === "withdrawn") return "已撤回";
  return "单一来源报道";
};

export const lifecycleLabel = (value: NewsLifecycle | string): string => {
  if (value === "emerging") return "新出现";
  if (value === "developing") return "发展中";
  if (value === "stable") return "稳定";
  if (value === "fading") return "降温";
  if (value === "dormant") return "休眠";
  return "重新激活";
};

export const analysisLabel = (status: string): string => {
  if (status === "available") return "AI 分析可用";
  if (status === "reused") return "复用已验证 AI 分析";
  if (status === "pending" || status === "claimed") return "AI 分析处理中";
  if (status === "failed") return "AI 分析失败";
  if (status === "insufficient") return "证据不足";
  return "尚无 AI 分析";
};
