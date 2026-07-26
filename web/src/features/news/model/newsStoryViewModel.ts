import type { components } from "@lib/types";

export type NewsStorySummary = components["schemas"]["NewsStorySummaryData"];
export type NewsStoryDetail = components["schemas"]["NewsStoryDetailData"];
export type NewsArticle = components["schemas"]["NewsArticleData"];
export type NewsVerificationStatus = NewsStorySummary["verification_status"];
export type NewsStoryPhase = NewsStorySummary["phase"];

export const verificationLabel = (status: NewsVerificationStatus): string => {
  if (status === "corroborated") return "多源核验";
  if (status === "trusted") return "权威来源";
  if (status === "attributed") return "仅有归因";
  return "尚未核验";
};

export const phaseLabel = (phase: NewsStoryPhase): string => {
  if (phase === "breaking") return "突发";
  if (phase === "developing") return "发展中";
  if (phase === "sustained") return "持续";
  return "热度消退";
};

export const analysisLabel = (status: NewsStorySummary["analysis_status"]): string => {
  if (status === "available") return "AI 已分析";
  if (status === "failed") return "AI 分析失败";
  if (status === "unavailable") return "AI 未配置";
  return "等待 AI 分析";
};
