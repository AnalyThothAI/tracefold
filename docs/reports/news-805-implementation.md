# #805 方案 A：实现与认证边界

[Issue #805](https://github.com/AnalyThothAI/tracefold/issues/805) · [News](../modules/news.md) · [契约](../CONTRACTS.md)

本次授权范围是实现、契约、消费者与离线工具。未执行真实模型重问、Claude 标注、生产重放、合并或部署。实现基线最终同步到 `32e585326f2e40fb248053ae38ec208356ac1049`，包括最新 Event 读者详情页和设计说明。

[机器可读实现状态](news-805-implementation.json)记录问题身份、资格表摘要、规范版本、未拟合系数、空切线及尚无认证数据这一事实，不充当认证报告。

## 可审阅的结果

- 模型分别回答报道类型、锚点、增量影响、是否应优先看到；冻结输入保持 v3。native 与 generated 保存同一证据结构，并分别命名判断器身份。
- 资格表是 owner 推送规则的代码入口。推送资格质量 `e`、影响尾部 `m`、打断概率 `i` 经过两个小型 logistic 模型得到 `p_push` 与 `p_key`。期望值只供展示；held 是输入，无单独切线。
- 计划保存原分布、confidence、策略分数与认证状态。HTTP 和页面使用冻结记录，旧 importance 证据保持历史展示，不能进入新判断缓存或校准集。
- 发送结算只消费已持久决定的元数据，不解析其模型证据；问题升级不妨碍旧意图完成结算。
- 离线工具按拟合、独立认证、报告分段处理，不写生产系数、数据库、缓存或通知。标注规范由 owner 规则独立版本化。

## 当前认证状态

| 后端 | 拟合数据 / 数据集摘要 | 系数 | 推送切线 / 重点切线 | 下界与有效标签 | 状态 |
| --- | --- | --- | --- | --- | --- |
| native | 未采集新问题金标 | 未拟合的零占位 | null / null | 未认证 | uncalibrated |
| generated | 未采集新问题金标 | 未拟合的零占位 | null / null | 未认证 | uncalibrated |

零系数产生的 0.5 只是未拟合占位，不代表推送或重点概率已校准。生产常量保持 `uncalibrated`，普通模型判断只进入信息流；已知、发送未决、更正、上币和大涨跌等既有确定性行保持原行为。**本 PR 不满足上线门槛，不能直接部署替换当前推送策略。**

单元测试中的系数、分布与认证状态均为显式合成 fixture，证明算法和接缝行为，不证明模型质量。#791 的真实分布保留原始问题与来源，不能被改名当作 #805 的真实分布。

## 后续质量证据

真实重问须另获授权。随后应分别为两个后端保存问题身份、输入摘要和模型身份；按时间及故事拆分拟合与认证集，使用带规范版本的 owner 金标。Claude 是代理标注，重叠集报告一致性和混淆矩阵，不以模型间一致性宣称准确率。

切线须在独立认证数据上证明单侧 Clopper–Pearson 下界，推送至少 0.65、重点至少 0.75，整体 δ = 0.1；推送至少 150 条有效认证标签，重点至少 60 条。无已认证切线、采样设计不支持证明或标签不足时工具报告未认证，不挑一个看似最好的切线。切线认证报告与机器 JSON 必须包含数据集摘要、规范版本、问题身份、系数、切线、目标、δ、下界及样本量。

同输入 v3 对照的区分度与召回、类型混淆、双峰比例、真实 p90、日量护栏以及上线 24 小时复核尚待完成。日量偏离 owner 目标时需要书面取舍，工具不按日量自动移动切线。

## 离线工具入口

冻结输入每行包含 `case_id`、`claim_ref`、`reader_input`、`message_intents`、当时的 `links` / `receipts`、`first_available_at_ms` 与采样元数据。owner 标签还需 `reader_input_sha256`、当前 `guide_version`、`labeler=owner`、`story_id` 和规范中的 `label`；认证用 owner 标签必须显式记录最终联合入样概率及采样设计，定向复核样本只用于拟合。Claude 和 owner 的原始标签日志分别保留，用于一致性报告。

以下命令只处理**已经获得**的冻结输入、标签和真实重问日志，不会调用模型。`/tmp/news-805/` 是操作者选定的离线目录示例。

```bash
uv sync --locked --group research
uv run --group research python -m scripts.eval_news_reader assemble \
  --input /tmp/news-805/frozen-cases.jsonl \
  --labels /tmp/news-805/claude-labels.jsonl --labels /tmp/news-805/owner-labels.jsonl \
  --native-journal /tmp/news-805/native-reasks.jsonl \
  --generated-journal /tmp/news-805/generated-reasks.jsonl \
  --output /tmp/news-805/dataset.jsonl
uv run --group research python -m scripts.eval_news_reader fit \
  --backend native --input /tmp/news-805/dataset.jsonl --output /tmp/news-805/native-candidate.json
uv run --group research python -m scripts.eval_news_reader certify \
  --input /tmp/news-805/dataset.jsonl --candidate /tmp/news-805/native-candidate.json \
  --output /tmp/news-805/native-certificate.json
uv run python -m scripts.eval_news_reader report \
  --artifact /tmp/news-805/native-certificate.json --output /tmp/news-805/native-report.md
uv run python -m scripts.label_news_reader report \
  --owner /tmp/news-805/owner-labels.jsonl --proxy /tmp/news-805/claude-labels.jsonl \
  --candidate /tmp/news-805/native-candidate.json --output /tmp/news-805/label-agreement.json
```

generated 使用匹配后端的重问日志与配对 v3 基线，另行组装、拟合、认证和报告。每行的 `baseline_v3` 只绑定一个后端，不能将 native 对照当作 generated 对照。`scripts.reask_news_models reader` 与 `scripts.label_news_reader annotate` 会发出真实调用，须另获授权，本次没有执行。

同输入 v3 对照记录单独放在 `baseline_v3` 中，带输入摘要、后端、原 v3 问题身份、adapter 身份、旧期望值 `rank_score` 与原 `pushed` 决定；裸分数无法满足配对对照。诊断报告提供类型混淆、召回与可用的逐次耗时 p90；缺少真实记录时明确为未知。按命题的日量估计不等于实际卡片推送量，实际 300–500 / 50–60 的护栏仍需按 Event / 卡片生产回放验证。

## 实现验证

最终命令与结果由实现 PR 记录。本报告不将合成回归、静态检查或待执行的 CI 计为真实模型认证。
