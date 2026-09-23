import { ActionButton } from "@shared/ui/ActionButton";
import { Card } from "@shared/ui/Card";
import { useState } from "react";
import { Link } from "react-router-dom";

import { useTradingAnalysisReplay, type TradingCase } from "../api/tradingQueries";
import { caseClock } from "../model/tradingLabels";

const factorName: Record<string, string> = {
  catalyst: "事件催化",
  price_structure: "价格结构",
  volume_and_oi: "成交与持仓",
  crowding: "拥挤度",
  entry_timing: "入场时机",
  trading_cost: "交易成本",
};

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function word(value: unknown): string {
  return value == null ? "—" : String(value);
}

export function TradingAnalysisDetail({ item, token }: { item: TradingCase; token: string }) {
  const [openReplay, setOpenReplay] = useState(false);
  const replay = useTradingAnalysisReplay(token, item.case_id, openReplay);
  const decision = record(item.analysis_decision?.decision);
  const assessmentReceipt = record(replay.data?.assessment);
  const assessment = record(assessmentReceipt?.assessment);
  const candidateAssessments = Array.isArray(assessment?.candidate_assessments)
    ? assessment.candidate_assessments
    : [];
  const evidence = record(replay.data?.evidence);
  const market = record(evidence?.market);
  const source = record(replay.data?.source_fact);

  return (
    <section aria-label={`分析案例 ${item.base_symbol}`} className="trading-case-detail">
      <Card
        flush
        title={`${item.base_symbol} · ${word(item.analysis_status)}`}
        hint="冻结的来源与分析状态"
      >
        <dl className="trading-case-facts">
          <div className="trading-case-fact">
            <dt>案例</dt>
            <dd>
              <code>{item.case_id}</code>
            </dd>
          </div>
          <div className="trading-case-fact">
            <dt>资产身份</dt>
            <dd>{item.target_asset_id ?? "未选定"}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>目标判定</dt>
            <dd>{word(item.target_selection?.reason)}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>来源时刻</dt>
            <dd>{caseClock(item.observed_at_ms)}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>决策时刻</dt>
            <dd>{caseClock(item.decided_at_ms)}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>发布状态</dt>
            <dd>
              {item.analysis_decision?.publish_status ?? "未发布"} ·{" "}
              {item.analysis_decision?.publish_reason ?? "—"}
            </dd>
          </div>
          <div className="trading-case-fact">
            <dt>入场范围</dt>
            <dd>
              <code>{item.entry_scope_id ?? "—"}</code>
            </dd>
          </div>
          <div className="trading-case-fact">
            <dt>执行记录</dt>
            <dd>
              <Link to={`/trading?tab=executions&execution_case=${item.case_id}`}>
                查看关联执行
              </Link>
            </dd>
          </div>
        </dl>
      </Card>

      <Card
        flush
        title="Agent 判断"
        hint={item.analysis_decision?.policy_version ?? "无有效模型判断"}
      >
        <dl className="trading-case-facts">
          <div className="trading-case-fact">
            <dt>动作</dt>
            <dd>{item.analysis_decision?.action ?? "—"}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>方向</dt>
            <dd>{word(decision?.side)}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>候选</dt>
            <dd>{word(decision?.selected_candidate_id)}</dd>
          </div>
          <div className="trading-case-fact">
            <dt>原始理由</dt>
            <dd>{word(decision?.reason)}</dd>
          </div>
        </dl>
      </Card>

      <Card flush title="后续机会路径" hint="标的价格路径；不代表订单成交或账户净收益">
        {item.analysis_outcomes?.length ? (
          <div className="trading-case-checks">
            {item.analysis_outcomes.map((outcome) => (
              <p key={`${outcome.axis}-${outcome.horizon_seconds}`}>
                {outcome.axis === "source" ? "来源反应" : "决策可用后"} ·{" "}
                {outcome.horizon_seconds / 60} 分钟 ·{" "}
                {outcome.status === "ok" ? `${outcome.return_bps} bps` : outcome.status}
                {outcome.labeled_at_ms ? ` · 标注于 ${caseClock(outcome.labeled_at_ms)}` : ""}
              </p>
            ))}
          </div>
        ) : (
          <p className="trading-inline-empty">没有适用的价格路径。</p>
        )}
      </Card>

      <Card flush title="冻结回放" hint="从原始归档读取，不重新调用模型">
        <ActionButton size="sm" onClick={() => setOpenReplay((value) => !value)}>
          {openReplay ? "收起回放" : "查看冻结回放"}
        </ActionButton>
        {openReplay && replay.isPending ? <p>正在读取归档…</p> : null}
        {openReplay && replay.isError ? <p>归档读取失败。</p> : null}
        {openReplay && replay.data ? (
          <div>
            {replay.data.status !== "ok" ? <p>回放状态：{replay.data.status}</p> : null}
            <p>来源：{word(source?.headline ?? source?.title ?? source?.kind)}</p>
            <p>证据截止：{caseClock(Number(evidence?.knowledge_cutoff_ms) || null)}</p>
            <p>
              模型：{word(assessmentReceipt?.model)} · 调用状态：
              {word(assessmentReceipt?.provider_status)} · 校验：
              {word(assessmentReceipt?.validation_status)}
            </p>
            <p>
              用量：输入 {word(assessmentReceipt?.input_tokens)} / 输出{" "}
              {word(assessmentReceipt?.output_tokens)} token · 实际费用{" "}
              {assessmentReceipt?.cost_microusd == null
                ? "供应方未返回"
                : `${word(assessmentReceipt.cost_microusd)} 微美元`}
            </p>
            <p>
              版本：提示词 {word(assessmentReceipt?.prompt_sha)} · 证据 profile{" "}
              {word(assessmentReceipt?.profile_version)}
            </p>
            <p>
              原始回执：请求 {word(assessmentReceipt?.request_ref)} · 响应{" "}
              {word(assessmentReceipt?.response_ref)}
            </p>
            {market
              ? Object.entries(market).map(([name, value]) => (
                  <p key={name}>
                    {name}：{word(record(value)?.status)} · {word(record(value)?.missing_reasons)}
                  </p>
                ))
              : null}
            {candidateAssessments.map((value, index) => {
              const candidate = record(value);
              const factors = Array.isArray(candidate?.factors) ? candidate.factors : [];
              return (
                <div key={word(candidate?.candidate_id ?? index)}>
                  <h4>{word(candidate?.candidate_id)}</h4>
                  {factors.map((raw, factorIndex) => {
                    const factor = record(raw);
                    return (
                      <p key={`${word(factor?.factor_id)}-${factorIndex}`}>
                        {factorName[word(factor?.factor_id)] ?? word(factor?.factor_id)} · 权重{" "}
                        {word(factor?.weight_bps)} bps · 支持度 {word(factor?.support_score)} ·{" "}
                        {word(factor?.status)} · 引用{" "}
                        {Array.isArray(factor?.evidence_refs)
                          ? factor.evidence_refs.join(", ")
                          : "—"}
                      </p>
                    );
                  })}
                </div>
              );
            })}
          </div>
        ) : null}
      </Card>
    </section>
  );
}
