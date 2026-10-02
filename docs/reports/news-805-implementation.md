# #805 方案 A：实现与认证边界

[Issue #805](https://github.com/AnalyThothAI/tracefold/issues/805) · [PR #808](https://github.com/AnalyThothAI/tracefold/pull/808) · [News](../modules/news.md) · [标注规范](../modules/news-reader-labeling.md) · [契约](../CONTRACTS.md)

本次范围是代码、契约、消费者、离线工具和切换说明，均在同一 PR。未执行真实模型重问、Claude 标注、生产导出或回放、合并、部署。实现基线为 `32e585326f2e40fb248053ae38ec208356ac1049`。

[机器可读状态](news-805-implementation.json)记录问题身份、资格表摘要、规范版本、未拟合系数、空切线和缺失真实认证的事实。它是实现状态，不充当 `news-805` 真实认证报告。

## 可审阅的结果

模型在同一次 JEV 请求中独立回答类型、增量影响、是否应优先看到和原有核心锚点，冻结输入仍为 v3。资格质量 e、影响尾部 m 和 held 输入推送 logistic，打断概率 i 与 e 输入重点 logistic；锚点不乘入分数。native/generated 分开拟合，m* 在较早故事的折外预测中选择。

计划冻结四题分布、confidence、概率、认证状态、当时推送/重点切线和校准身份。HTTP 与普通详情展示影响四档分布和冻结切线；`reader_ineligible` 与低概率、未认证分开。旧记录缺字段时为 null，旧 importance 证据保留原结构，只读展示。

[运行时校准文件](../../tracefold/news/notifications/reader_calibration.json)由 `ReaderPolicy.load()` 加载。文件字节摘要和资格表参与策略身份；可执行认证同时检查问题身份、生产组合判断器、实际作答适配器、served model 与激活审阅。离线证书通过 [导出桥接](../../scripts/export_news_reader_calibration.py)进入运行时格式，默认 `release_ready=false`，禁止命令直接覆盖生产文件。

## 当前认证状态

| 后端 | 数据 / 系数 | 推送切线 / 重点切线 | 状态 |
| --- | --- | --- | --- |
| native | 尚无新问题金标；零系数未拟合占位 | null / null | uncalibrated，未激活 |
| generated | 尚无新问题金标；零系数未拟合占位 | null / null | uncalibrated，未激活 |

零系数产生的 0.5 不是已校准概率。当前普通模型评分路径只进入信息流，既有确定性更正、上币、大涨跌等前置规则保持原行为。**本 PR 尚不满足生产策略切换门槛。** 合成 fixture 只证明算法与接缝；#791 的旧问题真实分布不能改名作为 #805 认证。

## 冻结数据与抽样

[导出器](../../scripts/export_news_reader_cases.py)只接受 `TRACEFOLD_READONLY_DSN`，在只读 repeatable-read 快照中按原采用 update、claim 和 ordered message intents 恢复冻结 ReaderInput。逐行核对原输入摘要及当时链接/回执，无法证明的历史状态会拒绝，不能借当前 head 或召回补造。输出 JSONL.gz 和 manifest（未压缩 canonical JSONL 的 dataset_sha256、总体、各层总量与 n/N）。决策带 × 已存类型分层；历史 v3 缺类型写 unknown，不猜类型或 held。

```bash
python -m scripts.export_news_reader_cases \
  --from-ms FROM_MS --to-ms TO_MS --per-stratum 50 --key-per-stratum 100 \
  --seed 805 --output /tmp/news-805/frozen-cases.jsonl.gz \
  --manifest /tmp/news-805/frozen-cases.manifest.json
```

reask 与 annotate 的追加日志输出必须为普通 JSONL；归档后可压缩为 gzip 输入。每行保存原决定、reader_applicable、pre_reader_reason 和 deterministic_decision。确定性行无需调用读者模型；reask 日志明确 skipped。完整流程的召回与覆盖保留失败、超时、缺失调用及确定性贡献，逐日估计保留零推送日期；成功回答上的区分度与概率指标另标为条件指标。没有成对真实耗时不能通过延迟门槛。

原始 claim_decision 抽样框不证明独立故事总体。当前 `owner-sample` 要求完整 claim census：框总数等于全部行数，原始入样概率全部为 1。应按实际各层总量设置导出数量并核对 manifest；缺失行不能伪造为 census。完整代理故事归组须在 owner push/key 标签前独立复核，然后工具按最近故事固定最早代表、各层质量、联合概率和抽中 ID。私有 manifest 记录完整 roster，盲标请求不含代理答案、旧决定或模型分数。证书总体仅为这些独立故事代表，不能扩大到全部 claim 或 Event/卡片。

## 拟合与独立认证

独立 owner 规则由 [news_reader_labeling.py](../../scripts/news_reader_labeling.py)生成 [规范](../modules/news-reader-labeling.md)，类型文本与生产选项同源，资格变化同时改变 guide_version。Claude 是代理标签，可参与拟合和一致性报告，不能计入 owner 认证标签。

先组装完整代理数据，再冻结较早时间/故事拟合与最近 holdout。候选绑定全部输入、回答、拟合标签、配置、m*、系数、折外预测、切线序列及 holdout；认证重新拟合验证 derivation，修改参数并重算自摘要也会拒绝。owner 金标允许在最近部分按预先固定的选择到来，保留代理原日志。每后端持久 holdout ledger 冻结一个候选与已读取金标数据，不能换样本反复试到通过；本地记录仍需 owner 审阅外部访问情况。

以下命令只处理已获得的文件，不调用模型；示例的 per-stratum 是预算参数，不能保证达到 150/60。完整故事归组未经独立审阅时省略 reviewed 参数，工具保持未认证。

```bash
uv sync --locked --group research
uv run --group research python -m scripts.eval_news_reader assemble \
  --input /tmp/news-805/frozen-cases.jsonl.gz \
  --labels /tmp/news-805/claude-labels.jsonl.gz \
  --native-journal /tmp/news-805/native-reasks.jsonl.gz \
  --generated-journal /tmp/news-805/generated-reasks.jsonl.gz \
  --output /tmp/news-805/proxy-dataset.jsonl.gz
uv run --group research python -m scripts.eval_news_reader fit \
  --backend native --input /tmp/news-805/proxy-dataset.jsonl.gz \
  --output /tmp/news-805/native-candidate.json
uv run --group research python -m scripts.eval_news_reader owner-sample \
  --input /tmp/news-805/proxy-dataset.jsonl.gz --candidate /tmp/news-805/native-candidate.json \
  --per-stratum 200 --seed 805 --story-grouping-reviewed \
  --output /tmp/news-805/blind-owner.jsonl.gz --manifest /tmp/news-805/owner-selection.json
uv run python -m scripts.label_news_reader prepare-owner \
  --input /tmp/news-805/blind-owner.jsonl.gz --output /tmp/news-805/owner-text.jsonl.gz \
  --manifest /tmp/news-805/owner-text.private.json
# 只交付 owner-text；human-labels 每行填写 case_id、blind_input_sha256、story_id 和 label。
uv run python -m scripts.label_news_reader import-owner \
  --input /tmp/news-805/blind-owner.jsonl.gz --manifest /tmp/news-805/owner-text.private.json \
  --labels /tmp/news-805/human-labels.jsonl.gz --output /tmp/news-805/owner-labels.jsonl.gz
uv run --group research python -m scripts.eval_news_reader assemble \
  --input /tmp/news-805/frozen-cases.jsonl.gz \
  --labels /tmp/news-805/claude-labels.jsonl.gz --labels /tmp/news-805/owner-labels.jsonl.gz \
  --native-journal /tmp/news-805/native-reasks.jsonl.gz \
  --generated-journal /tmp/news-805/generated-reasks.jsonl.gz \
  --output /tmp/news-805/gold-dataset.jsonl.gz
uv run --group research python -m scripts.eval_news_reader certify \
  --input /tmp/news-805/gold-dataset.jsonl.gz --candidate /tmp/news-805/native-candidate.json \
  --holdout-ledger /tmp/news-805/holdout-uses.jsonl --output /tmp/news-805/native-certificate.json
uv run python -m scripts.eval_news_reader report \
  --artifact /tmp/news-805/native-certificate.json --output /tmp/news-805/native-report.md
```

generated 用匹配的日志、候选与 owner 抽样框分别完成同一流程。同输入 baseline_v3 必须携带输入摘要、后端、旧题目身份、adapter、rank_score、实际 pushed 和耗时，不能借另一后端或裸分数证明成对质量。标签一致性使用 `scripts.label_news_reader report`，分别保留 owner/proxy 日志。`reask_news_models reader` 与 `label_news_reader annotate` 会真实调用，本次未执行。

切线为预先固定序列，推送单侧精度下界至少 0.65、重点至少 0.75；切线之上至少 150/60 个独立 owner 故事。每后端联合 push/key 的 family δ=0.1；重点对所有可能推送切线的条件选择作多重检验保护，不宣称两个后端合并也具有该 δ。等概率直接使用 Clopper–Pearson；分层设计用同时有效的逐层联合正例下界和选择质量上界、冻结各层总体质量构造保守比率，不用有效样本量或小数权重伪造二项试验。

## 证书进入运行时与切换

精度证书与完整 release gates 分开。导出时绑定当前 guide、问题、资格表、真实作答来源、数据/候选/holdout 摘要和报告字节摘要。生产组合判断器 identity 必须明确提供：离线 forced-generated 的 program identity 不能替代生产 native/fallback 组合。

```bash
uv run python -m scripts.export_news_reader_calibration \
  --native-certificate /tmp/news-805/native-certificate.json \
  --generated-certificate /tmp/news-805/generated-certificate.json \
  --native-report /tmp/news-805/native-report.md --generated-report /tmp/news-805/generated-report.md \
  --reader-identity REVIEWED_PRODUCTION_COMPOSITE_IDENTITY \
  --output /tmp/news-805/reader_calibration.review.json
```

以上导出仍未激活。激活需 `--review` 的 native/generated 审阅记录分别绑定 dataset_sha256、candidate_identity、holdout_identity、canonical certificate_sha256、报告字节 report_sha256、reader_identity、reviewed_by 与含时区 reviewed_at。全部测量门槛须为 true。external_gates 的 event_card_daily_replay、volume_targets_or_explicit_owner_waiver、owner_review、holdout_usage_review 均须有 passed=true、evidence_ref 和 evidence_sha256；它们是审阅的外部证据，不由工具制造。运行时文件冻结 review_ref 和每后端审阅记录的 canonical review_sha256，使外部证据变化进入校准身份并可追溯。

真实类型混淆、成对区分度与完整流程召回、双峰、p90、实际 Event/卡片 300–500 / 50–60 日量与 owner 取舍尚待采集。约 200 条 owner 标签需在标注前重新分配预算，不能按 70/30 后只剩约 60 条认证还宣称达到 150。完整上线证据后按 [切换与对称回滚](../OPERATIONS.md#news-reader-switch)操作：先用旧版本收敛已有尝试，停止 Workers，拒绝 sending、有效 lease 和尝试历史，再清除仅未发送的 pending 预留/卡片并唤醒重新规划。保留 immutable plan、sent/ambiguous 和失败预算，无 schema 迁移。上线 24 小时复核仍是独立证据。

## 实现验证

PR 记录实际静态、单元、PostgreSQL、浏览器与 CI 结果。合成端到端烟测包含 60 个超时的 240 个读者适用 owner 例：完整流程新召回 0.75 对旧 1.0，覆盖与 p90 门槛失败，精度通过也保持 release_ready=false。它验证失败分母与阻断逻辑，不证明真实模型表现。
