# #805 推送认证批 1：native 推送证书

[Issue #805](https://github.com/AnalyThothAI/tracefold/issues/805) · [PR #808](https://github.com/AnalyThothAI/tracefold/pull/808) · [实现状态](news-805-implementation.md) · [标注规范](../modules/news-reader-labeling.md) · [运行时校准文件](../../tracefold/news/notifications/reader_calibration.json)

本页只记录聚合数字、身份摘要和复现步骤，不含新闻正文、标签明细或私有路径。全部数字由本 PR 的工具重新计算，
与 owner 盲标后的临时计算逐项一致。没有调用模型；输入都是已存的本地文件。

## 结论

| 后端 | 推送 | 重点 | 运行时状态 |
| --- | --- | --- | --- |
| native | 切线 0.372 认证通过，单侧下界 0.783 | 未认证：候选重点区只有 56 个故事，不足 60 | `certified`，`key_cut = null`，没有任何命题会成为重点 |
| generated（回退） | 未重问、未认证 | 未认证 | `uncalibrated` 占位，回答只进信息流 |

运行时文件在 owner 发布审阅前保持 `release_ready=false`，此时 native 仍按未认证处理；审阅记录写入后才会推送。
交易范围规则（D12–D14）只在标注规范中，运行时不执行，下一轮写进读者问题后重新认证。

## 候选

- 数据：2,500 条概率样本（2026-09-29 至 10-02，按当时的生产决定与期望值分层，记录入样概率），guide v5 代理标签，
  native 重问回答（`typesafe/jev-1.13-20260917`，2,500/2,500 可用）。
- 资格表：`recap_or_old_period` 可推，摘要 `0608ae6a…25c`；问题身份 `f98af694…c4e0`（读者输入 v3）。
- 时间与故事切分：边界 2026-10-01 17:29:49 UTC；拟合 1,721 条（训练 1,627 条，去掉确定性和 borderline），
  留出 676 条，跨边界故事 11 个排除。
- 选择 m* = 2：推送折外 AUC 0.777、校准斜率 0.94；重点折外 AUC 0.926。
- 系数：推送 `(-1.1503, 0.0777, 0.4512, -0.7680)`（截距、logit e、logit m、held），
  重点 `(-6.8601, 2.2390, 0.0684)`（截距、logit i、logit e）。
- 复现：本 PR 的 `fit` 在同一数据上得到逐位相同的切分边界、系数、折外预测和切线序列
  （推送 0.471 → 0.0，重点 0.012 → 0.0）。唯一的输入差异是补齐导出器字段 `reader_applicable` 等三项
  （样本中全部为读者适用，训练集不变）。候选身份 `296d8c56…2b34b`，guide v5。

## 认证框与冻结选择

- census：2026-09-29 07:54 至 2026-10-03 07:47 UTC 之间得到读者回答的全部命题，每条取首次决定，共 7,542 条
  （窗口内 7,547 条，5 条的原输入无法复原而未导出）。窗口跨过边界，用于完整的故事归组。
- 故事：命题所属 Event 与已记录命题链接在整个 census 上取并集。边界之后 1,693 条命题、766 个故事；
  有成员早于边界的 32 个故事整体排除。每个故事取最早的一条作代表，766 个代表都适用读者判断并有 native 回答。
- 冻结选择（看标签之前，只用分数）：

| 层 | 定义 | 总体 | 抽中 |
| --- | --- | --- | --- |
| push_region | 资格通过且 `p_push ≥ 0.372` | 244 | 244（全查） |
| key_region | 资格通过、`p_key ≥ 0.012` 且不在推送区 | 0 | 0（上限 60） |
| rest | 其余 | 522 | 30（种子 805） |

- 工具核验：`owner-sample --frozen-selection` 重新计算故事框和 766 个代表的分数，与冻结文件逐条一致；
  同一种子重新抽样得到完全相同的名单。仓库生成的盲标材料与 owner 实际看到的材料内容一致（仅消息顺序不同）。

## owner 标签

- 274 条，全部按 guide v5 标注，带版本号和时间戳。owner 只回答推送和重点（D7）；类型与锚点取同一条的代理标签。
- 推 219、不推 53、borderline 2（按不推计）；重点 62。代理判为重复的 4 条，owner 全部确认，最终为不推。

## 推送证书

切线序列取候选冻结序列中不低于 0.372 的部分。最严的 0.471 以上只有 143 个故事（不足 150），
按分数确定检验从 0.417 开始；由严到松，首次失败即停。

| 切线 | 选中故事 | owner 标注 | owner 推 | 精度 | 单侧下界（δ=0.05） | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| 0.417 | 200 | 200 | 170 | 0.850 | 0.802 | 通过 |
| 0.372 | 244 | 244 | 202 | 0.828 | 0.783 | 通过 |

两条切线的选中故事都在全查的推送区内，下界就是单侧 Clopper–Pearson。结果与 owner 盲标后的临时计算一致
（0.802 / 0.783）。临时计算的敏感性分析：去掉热身见过的 6 条为 0.799 / 0.780；把农产品和小经济体条目改判为
不推为 0.769 / 0.752，均高于 0.65。

## 重点

推送切线 0.372 之内，重点候选（`p_key ≥ 0.012`）只有 56 个故事，不足 60，重点序列不检验，`key_cut = null`。
这 56 个故事中 owner 判为重点的有 23 个（0.41），远低于 0.75。重点认证留到下一轮。

## 其余测得门槛

| 门槛 | 结果 | 说明 |
| --- | --- | --- |
| 回答覆盖 | 通过 | 274/274 有 native 回答，无失败 |
| 类型误判不可推 ≤ 5% | 未通过 | owner 推的条目中 17.7% 的最可能类型不可推；10.5% 的资格质量低于 0.3 |
| 影响程度含糊 < 20% | 未通过 | 29.8% 的影响概率落在 0.35–0.65 |
| 成对区分度 | 无法测量 | 本框多数代表没有同输入 v3 记录；owner 加权 push AUC 0.716、重点 AUC 0.865 |
| 端到端召回不低于 v3 | 无法测量 | 同上；加权召回估计新策略 0.41，同框生产实际决定 0.32（描述性，非成对对照） |
| 成对 p90 延迟 | 无法测量 | 只有离线重问耗时（p90 348 ms），没有同输入旧耗时 |

这些门槛和四项外部证据（卡片日量回放、日量目标或 owner 豁免、owner 审阅、留出使用审阅）由 owner 在发布审阅中
逐项给出证据或书面豁免；精度证书和概率抽样不能豁免。

## 日量

- 工作日估计：用 2,500 条加权样本（3.4 个工作日）按候选分数折算，切线 0.372 约每天 511 条命题。
  发出的卡片平均约含 1.3 条命题，约每天 393 张卡，在 D1 的 300–500 之内。这是命题加权估计，不是 Event/卡片回放。
- 认证框内的精确数：1.6 天的留出框中 244 个故事代表在切线之上（证书 `volume`）。故事代表与卡片口径不同。

## 身份摘要

| 项 | 值 |
| --- | --- |
| 候选身份 | `296d8c566933ab90d2a271f533a5d201275111b547d3399cbbbb5d107eb2b34b` |
| 拟合数据 | `877b199b1f96c86af615aa8e323e3acf890de175ad1fda2770da53b1845175c3` |
| census | `aac1d42eacad88857dfd1aa20681e7d92ffdeb79bc7e4bc0f6f7f8aeb424d628` |
| 冻结选择 | `310a7a3a09192b278cd07648d4446f10495f7c8e6bc65bf0da52379df9a7d66d` |
| owner 标签 | `0b5d4ad852900a7dbc8d684465fe09ff79f1f502557c4c4f7dda515cba98ca94` |
| 认证数据集 | `55d28372c8ca34a2c5d34d708bafbb2e99933cd3382f7e9c74fc6ae0f5c8992c` |
| 留出身份 | `92bbf4058d84484c50c5438077906314b0f9d32cfee20b7fd7cef8ccfc32e94b` |
| 证书摘要（canonical） | `5248a9cea38e78788814e3c804f0a06e827784b3d1c51c8631b72a22daa9dea3` |
| 标注规范 | `news_reader_owner_guide_v5:d7aeb8783ad604167e29ba1c63115b6c896c4d3388f26f4320a924052b2da307` |

## 复现

全部命令只读写本地文件，不调用模型；`fit`、`owner-sample` 与 `certify` 需要 `uv sync --locked --group research`。
研究输入来自一次只读导出，字段与本 PR 的导出器一致的部分直接使用，其余由一份格式映射脚本从导出时的生产决定
旁表补齐：`event_id`、`update_ref`、`decided_at_ms`、原决定与原因、`reader_applicable`/`pre_reader_reason`/
`deterministic_decision`（原因为读者原因即适用）、census 抽样框（每条入样概率 1）、v3 时段 `verified_original`
与更早时段 `rebuilt` 的输入来源，以及抽中 274 条的被引用原文。代理标签的锚点按私有映射还原为原消息顺序。

```bash
uv run --group research python -m scripts.eval_news_reader fit --backend native \
  --input FIT.jsonl.gz --output CANDIDATE.json
uv run --group research python -m scripts.eval_news_reader owner-sample \
  --census CENSUS.jsonl.gz --journal NATIVE_BATCH.jsonl --journal NATIVE_CERT1.jsonl \
  --fit-input FIT.jsonl.gz --candidate CANDIDATE.json --frozen-selection FROZEN.json \
  --output SELECTION.jsonl.gz --manifest SELECTION.json
uv run python -m scripts.label_news_reader prepare-owner \
  --input SELECTION.jsonl.gz --output BLIND.jsonl.gz --manifest BLIND.private.json
uv run python -m scripts.label_news_reader import-owner \
  --input SELECTION.jsonl.gz --manifest BLIND.private.json \
  --labels OWNER_ANSWERS.jsonl.gz --proxy PROXY.jsonl.gz --output OWNER_LABELS.jsonl.gz
uv run --group research python -m scripts.eval_news_reader certify \
  --census CENSUS.jsonl.gz --journal NATIVE_BATCH.jsonl --journal NATIVE_CERT1.jsonl \
  --fit-input FIT.jsonl.gz --candidate CANDIDATE.json --selection SELECTION.json \
  --labels OWNER_LABELS.jsonl.gz --holdout-ledger LEDGER.jsonl --output CERTIFICATE.json
uv run python -m scripts.eval_news_reader report --artifact CERTIFICATE.json --output REPORT.md
uv run python -m scripts.export_news_reader_calibration \
  --native-certificate CERTIFICATE.json --native-report docs/reports/news-805-certification.md \
  --reader-identity PRODUCTION_COMPOSITE_JUDGE_IDENTITY --output reader_calibration.review.json
```

`--reader-identity` 是生产规划器组合判断器的身份；native 重问用与生产相同的组合方式构造，
其程序身份为 `news_reader_judge:33f09c44b4deec6fa6892485308b5c2dfd9a72030dd1c3602f3cbbfb828c1677`，
部署后须核对规划器实际身份与之相同，否则运行时按未认证处理。
