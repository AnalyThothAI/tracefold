# Issue #112 落地审计：代码完成不等于生产闭环完成

日期：2026-08-21（Asia/Taipei）
审计对象：`AnalyThothAI/tracefold#112`、当前工作树、生产库只读状态
结论：**可以按 #112 继续上线；现在不能关闭 #112，也不能声称 Agent 已经学会。**

## 1. 最短答案

这套系统现在已经有了“学习闭环的机器”，但还没有跑完“第一次真实学习”。

- 代码层：FactUnit、不可变证据、真实 sent receipt、ReviewDesk、CandidateEvaluator、双臂顺序 replay、未来留出集、shadow、单臂 canary、回滚凭证与复盘页 hard cut 已经落到工作树。
- 本轮补齐的发布门：固定 Agent cohort、显式标记 mutable model alias、50-cluster 预注册盲测、100 次人工预算耗尽返回 `UNKNOWN`、候选新增 critical error 直接 `FAIL`、前序阶段未 PASS 时禁止花下一阶段模型预算；0288 还实现了 90/365 天 bounded retention、当前/上一 stable pin、冷 Janitor 与状态指标（[#118](https://github.com/AnalyThothAI/tracefold/issues/118)）。
- 生产层：数据库仍为 `0283`，代码目标为 `0288`；生产没有 accepted review、eventless miss、未来 holdout、shadow、canary 或 rollback drill。这里没有任何数据可以诚实证明 precision、recall 或读者价值提高。

因此，正确状态不是“完成”或“失败”，而是：

```text
机制代码基本就绪
  -> 等待受控迁移
  -> 收真实人工证据
  -> 跑第一个 DRAM Prompt candidate
  -> 未来留出集
  -> 24h shadow
  -> 24h 10% canary
  -> 人工 promotion 或 rollback
  -> 把完整 receipt chain 回填 #112 后才能关闭
```

## 2. 高中生版本：生产闭环怎样跑

把每条新闻想成一道考试题。

1. **拆题**：一个 provider 消息如果明确包含多个编号事实，程序先拆成多个 `FactUnit`。模型每次只答一道题。
2. **封卷**：`EventEvidenceSnapshot` 保存模型当时真正看到的题。后来来了更强证据，只追加 v2，不篡改 v1。
3. **答题**：稳定版模型只做一次语义判断，输出事实、资产、方向、量级、标题和 why。
4. **校规**：确定性 policy 检查结构化语义条件与同事实重复；policy v7 没有每小时、2 小时或 4 小时读者额度。符合 push/escalate 条件就交给投递层，历史数量只能观测，不能否决。
5. **快递回执**：只有真实 `sent` 才算读者收到；pending、ambiguous 和模拟送达是不同概念。
6. **判卷**：operator 在复盘页先看冻结证据，不看价格，按 factual、asset、direction、magnitude、novelty、headline、why、timeliness 分维度判断。
7. **找第一处错误**：代码错误改程序，产品边界改 reader contract，语义边界改 Prompt，长期外部事实才进 versioned knowledge/retrieval；Codex Skill 只教维护 Agent 怎样执行流程。
8. **出候选卷**：一次 candidate 只能改 Prompt 或 Policy 中的一个变量，并保存 ProposalReceipt。
9. **旧题考试**：stable 与 candidate 在同一冻结数据上运行；任何已知 must-push、事实幻觉、错资产或严重重复回归都拦截。
10. **未来新题考试**：candidate 注册后才开始收未来数据；至少 24 小时、200 个 eligible Events，预先固定 50 个独立事实簇做匿名 A/B，最多花 100 次人工判断。
11. **影子运行**：candidate 读取真实生产证据，但只写学习表，不发消息、不改正式 verdict。
12. **小范围上线**：10% 低风险 Event 在模型调用前固定分臂；每个 Event 只调用一个模型、最多发一张卡。canary 只证明运行安全，不证明质量因果提升。
13. **人工升版**：人通过正常 Git review/image deploy 升 stable，并保留上一镜像至少 24 小时；出问题就回滚并保存 receipt。

这就是 Chapter 9 的核心：不是让 Agent 边跑边改自己，而是让每次改变都经过可复现的证据、比较、发布和回滚。

## 3. Issue #112 验收矩阵

### 3.1 Truth / data

| 要求 | 当前状态 | 证据/说明 |
|---|---|---|
| verdict 固定 `evidence_version` | 已实现 | `0284`、`repository.py`、pipeline integration tests |
| observed ledger 只含真实 sent | 已实现 | reader receipt 与 reservation/simulated/ambiguous 分离 |
| stronger evidence 只追加、不在线重判 | 已实现 | evidence v2 可审计；V1 不重发 |
| eventless miss 进入 Review v2 | 已实现 | ReviewDesk external miss 原子提交与测试 |
| judgment append-only、supersede/accept | 已实现 | `0285`、idempotency/correction tests |
| freeze 使用 DB clock + grace | 已实现 | 未终态 decisioned push 不冻结为 sent truth |
| reader contract 进入 case/dataset/report | 已加固 | DatasetManifest/Report 显式保存版本；不只依赖间接 SHA |
| Agent cohort 不混 | 已加固 | Dataset 只接受 verdict trace 中与 stable `bundle_sha` 完全一致的 Event；manifest 保存 prompt/schema/retrieval/model/execution/policy SHA |
| mutable model alias 不冒充快照 | 已加固 | ArmManifest 区分 `mutable_alias` 与 `immutable_revision` |

### 3.2 CandidateEvaluator

| 要求 | 当前状态 | 证据/说明 |
|---|---|---|
| public 只有 `freeze_dataset/evaluate` | 已实现 | CLI 只经 package-root interface |
| V1 Prompt/Policy 单变量 | 已实现 | Model/Retrieval/Program fail before model call |
| Prompt stable/candidate 真调用 | 已实现 | 固定 model/schema/retrieval/execution/policy，保存 request/response recording |
| Policy development 0-model cheap screen | 已实现 | integration test 固定 calls=0 |
| 两臂独立顺序 reader ledger | 已实现 | 每臂 deque，按 Event 顺序推进 |
| 热路径无读者数量额度 | 已实现 | policy v7 删除 hourly/asset/theme/flood caps；离线双臂也不再模拟额度，只报告 reader load |
| registration 早于 validation | 已实现 | candidate registration receipt + temporal validation check |
| strict record/replay | 已实现 | miss 不 live fallback；共同 provider outage 为 `UNKNOWN` |
| progressive gate | 已加固 | offline/holdout/shadow 未 PASS 时，下一层在模型调用前拒绝 |
| 预注册 N 与人工预算 | 已加固 | validation 在看输出前固定最多 50 个 cluster representative；100 judgments 未解决为 `UNKNOWN` |
| critical safety gate | 已加固 | A/B critical error 绑定到具体匿名 arm；candidate-only unsupported fact/wrong entity 等直接 `FAIL` |
| hidden primary cluster interval | 已实现 | cluster-level paired bootstrap；lower bound `<=0` 为 `UNKNOWN` |
| reaction 不生成 release truth | 已实现 | high-reaction 仅 discovery，`release_eligible=false` |
| trusted root 与 stale stable | 已实现 | active stable 变化只让 eligibility stale，不改历史 artifact |

### 3.3 ReviewDesk / UI

| 要求 | 当前状态 | 证据/说明 |
|---|---|---|
| 默认是可操作 queue | 已实现 | `/news/review` 四视图 hard cut |
| submit 返回 receipt + next | 已实现 | typed mutation hook、If-Match、idempotency |
| 匿名 A/B 不泄露 arm/outcome | 已实现 | security-barrier pairwise view；review role 看不到 base mapping |
| 页面能标 critical error | 已加固 | 现在可分别标 A/B 的事实、实体、方向、漏事实、严重重复、注入服从 |
| coverage 显示 N/比例/区间/strata | 已实现 | 空证据显示“证据不足”，不显示绿色 PASS |
| event rubric / pairwise / external miss | 已实现 | 页面不可 promote/canary/rollback |
| Market Reaction 独立且非因果 | 已实现 | 无 HIT hero、无 event-type quality 排名、maturity/cohort/fact-cluster 修正 |
| 详情删除复制 label 命令与错误价格暗示 | 已实现 | 非 primary 同名资产 reaction 隐藏并解释 |
| 精确响应式验收 | 已加固 | Playwright 固定 390×844 与 1366×720 |

### 3.4 Security / operations

| 要求 | 当前状态 | 证据/说明 |
|---|---|---|
| serve 只读；review 窄写 | 已实现 | role grants/audit tests；review role 不能读 base business/arm mapping |
| bearer + exact JSON | 已实现 | query token 不可 mutation；reviewer/source/arm 服务端拥有 |
| review 写故障不影响 readiness | 已实现 | 独立 one-slot review pool，readiness 只报告 availability |
| shadow 不碰正式真相 | 已实现 | 只写 learning/model-recording tables |
| canary 一 Event 一 arm/一卡 | 已实现 | 模型调用前 durable assignment，无同步 stable fallback |
| canary CAS、trip、restart safety | 已实现 | immediate/rolling/manual hold tests |
| content-addressed receipts | 已实现 | dataset/candidate/report/release/deployment/rollback artifacts |
| previous image + 24h rollback | 已实现 | runtime manifest 变更追加 deployment receipt |

### 3.5 不能由代码测试替代的真实证据

下面全部仍未完成，因此 #112 必须保持打开：

- 30 boundary、100 retention、50 negative 独立事实簇和完整 safety set；
- 页面真实 POST → coverage → freeze → evaluator 的生产证据链；
- DRAM failure cluster 的单变量 Prompt candidate；
- candidate 注册后的未来 24h/200 Event hidden holdout；
- 50-cluster 匿名判断与 `95% lower bound > 0`；
- 24 小时 shadow 的 schema/degraded/latency/cost 证据；
- 10% low-risk canary 至少 24 小时；
- 人工 promotion 或 rollback drill 及 previous-image receipt。

## 4. 两条重点漏推怎样进入闭环

### 4.1 DRAM

`0fefa7b402187b3247d3d162f71910b7b0e08b98bbb55d0c078f15a0049d32fc`

- 当时 Gate 正常、无节流、无投递故障；SemanticJudge 主动给 `m1/actionable=false/drop`。
- “已被预期覆盖”没有 source 或 told evidence，是 unsupported dismissal。
- 该 case 应进入 `sector_fundamental_update` boundary，first bad owner 是 Prompt/semantic rubric。
- candidate 只改 Prompt：明确“priced-in/expected/covered 必须有 source 或 told ref；新行业定价/供需数据不能仅因 preliminary/scheduled 自动降成 m1”。
- 不要把“DRAM 永远推”写成关键词或 Skill。

### 4.2 GLM-5.3

`da2e8ef353563ee6e011e9764ffb0e6a104d647f94ff7501520cc4c66c72135d`

- 模型正确识别 Z.ai GLM 不是 GLM token；按当前 reader contract，“无直接可交易标的”应 drop。
- 如果 operator 仍定义它为重点，必须先版本化修改 reader contract，引入 `strategic_technology_frontier`，再重建 baseline；不能与 DRAM Prompt candidate 混成一次实验。
- GLMUSDT 的同名价格不是该 AI 模型事件反应；页面现在默认隐藏非 primary reaction。

## 5. 历史数据怎样用，价格怎样不用

固定 24 小时审计能证明运行事实，不能自动证明内容好坏：

- 1628 Events、1572 triaged、419 sent；labels/eventless misses 均为 0；
- 24 小时混合四个 Prompt/Policy cohort，所以顶线不是当前 Agent 的质量率；
- delivered 的 1h 方向 sign hit 约 50.2%，held 约 50.3%，且覆盖不同；这不是因果，也看不出 selection lift；
- 大量宏观/中东主题、每小时约 17.5 张卡和短间隔推送提示“读者负载”值得复盘；
- 多事实 digest、storyline 正则、批量模板重复可以由代码证据直接判为 correctness issue；其余卡片必须由人按 rubric 判断。

正确用法：历史数据用于找 failure cluster、建立 development/retention/negative/safety 集；真正批准 improvement 只能用 candidate 注册后未来发生、且候选生成器没有看过的 temporal holdout。

用户现有 Claude 复盘 Agent 可以继续用，但它的角色只能是：生成逐卡初稿、归类 owner、提出 candidate hypothesis。它不能自动写 accepted judgment、不能看 hidden arm mapping、不能改 trusted root、不能 promote。

## 6. Prompt、知识、程序还是 Skill

| 问题 | 正确沉淀位置 |
|---|---|
| “priced in” 无证据、行业基本面量级误判 | Prompt + reviewed boundary examples |
| 私营 AI 模型是否值得推 | reader contract + rubric |
| `STRAITS`/Guyana oil storyline 误分 | deterministic code + regression test |
| GLM 模型与 GLM token 同名 | entity resolver/code + test |
| 正式法规定义、稳定行业术语 | versioned knowledge/retrieval source |
| 如何冻结、跑 evaluator、发布证据 | Codex Skill / operator runbook |
| 某一条新闻应该推 | ReviewJudgment，不写成永久关键词知识 |

## 7. Build vs Buy 的最终边界

- **必须自建**：ReviewDesk、真实 sent truth、FactUnit/EvidenceSnapshot、顺序双臂 ledger、candidate-unseen temporal holdout、single-authority canary、deployment/rollback receipt。这些是 Tracefold 领域合同，通用库没有。
- **可以包裹**：Promptfoo 仅作为 dev/CI 的通用执行与断言 runner；OTel/Phoenix 可作为只读观测。它们不能成为 dataset/report/control 的第二 truth。
- **以后再用**：GEPA/DSPy 只能在 evaluator 可信后做 development candidate search，永不自动批准。
- **不采用**：任何 nightly 自改 Prompt、自改 trusted root 或自动 promote 的“持续进化”框架。

## 8. 生产 rollout：每一步都能停

### Phase 0：迁移前

1. 固定 Git SHA、镜像 digest、当前 `0283` schema head、备份与回滚命令。
2. 在生产快照的隔离数据库演练 `0283 → 0288`，核对 legacy label count/hash、role grants、query plans 与 learning-retention backlog。
3. 明确 review bearer 与 `tracefold_review` 连接；禁止复用 workers/serve 凭据。

### Phase 1：只上证据与 ReviewDesk

1. 迁移数据库，部署 stable-only image；canary 保持未 armed。
2. 验证 ingest/triage/delivery/readiness 与迁移前基线一致。
3. 复盘页开始真实提交；先收 DRAM、已知多事实错位、错资产、重复、随机 held/delivered。

### Phase 2：冻结 development 与候选

1. 达到最低独立事实簇后冻结 closed development window。
2. 只生成 DRAM `why_support/sector_fundamental` Prompt candidate；GLM reader-contract 变化另开实验。
3. offline 未 PASS，不得冻结或运行下一层。

### Phase 3：未来留出集

1. candidate 注册之后才开始计时。
2. 满足 ≥24h、≥200 eligible Event 后 freeze validation。
3. 系统固定最多 50 个独立 cluster 做 blind review；最多 100 次判断。证据不足、uncertain、预算耗尽都为 `UNKNOWN`。

### Phase 4：shadow 与 canary

1. 24h shadow 只写 learning artifacts；检查 schema、degraded、latency、tokens/cost、reader volume、duplicate。
2. shadow PASS 后 arm 10% low-risk canary；至少 24h。
3. 任何 hash/schema/one-arm breach 立即 trip；rolling error SLO 连续越界回 stable；当前 Event 不双跑。

### Phase 5：人工发布或回滚

1. 人工核对 active stable 仍等于 candidate parent。
2. 正常 Git/image deploy；保存 previous stable image digest，至少 24h 可回滚。
3. 做一次真实 rollback drill；把所有 artifact SHA、窗口、N、区间、成本、assignment 和 receipt 链贴回 #112。

## 9. 上线影响推演

立即会改善：复盘对象不再漂移；错题能路由到正确 owner；价格不再被当 reward；Prompt candidate 能被真实模型与未来数据验证；错误版本可以回滚。

不会立即改善：推送质量不会因为迁移瞬间变高。刚上线时更可能看到大量 `UNKNOWN`，这是系统停止伪造确定性的正常现象。

成本：人工需要先完成约 180 个 development 独立事实簇的多维证据，首个 validation 计划 50 个盲测 cluster；Prompt candidate 会产生 stable/candidate 真模型调用，shadow 还会增加一条冷模型成本。

主要风险与保护：

- review adoption 再次归零：默认 queue、提交并下一条、覆盖缺口可见；
- reviewer 被候选风格带偏：validation A/B 隐藏 arm、diff、价格和目标方向；
- candidate 改变读者负载：stable/candidate 的每小时均值与峰值只作为报告证据，不能阻止一个已经满足语义条件的独立事实发送，也不能单独阻止候选发布；
- mutable model alias 漂移：报告明确标为 mutable，不声称 exact snapshot；
- canary 造成双发：分臂在模型前持久化，每 Event 一 arm，无同步 fallback；
- 结果不显著却想上线：interval 跨 0、证据不足或预算耗尽一律 `UNKNOWN`。

## 10. 仍需后续工程票，不应伪装成已完成

这些不阻塞第一轮受控上线，但不能从架构债务表消失：

1. development pairwise 目前保持匿名，不提供正文所设想的“单题提交后 reveal exact diff”；这不削弱 blind gate，但降低诊断便利性。
2. storyline `strait` 词界与 `oil` 中东上下文见 [#116](https://github.com/AnalyThothAI/tracefold/issues/116)，taxonomy v2 见 [#117](https://github.com/AnalyThothAI/tracefold/issues/117)，Price resolver class-aware/grace 见 [#119](https://github.com/AnalyThothAI/tracefold/issues/119)。旧 `first_push_delay_min_p50` 已随 legacy offline evaluator 删除，不再另建兼容债。
3. 生产首个 proof 前，不应接 Prompt optimizer、GEPA/DSPy 或第二套 observability truth plane。

## 11. 最终代码验证（仍不是生产效果证明）

本工作树在目标 schema `20260821_0288` 上完成了以下回归；这些数字证明实现没有破坏既有合同，不能替代未来真实 holdout 与 canary：

- 后端全量（含 integration/e2e/golden/slow real-process、`0283 → 0288` 有数据迁移演练与旧卷 review-role 引导）：`580 passed`；
- 前端 Vitest：`162 passed`；ESLint + 前端架构：`76 passed`；production build 与 Prettier：PASS；
- Playwright 四种视口：`63 passed`、`53 skipped`（按 project/viewport 条件跳过），四张 golden snapshot 复核通过；
- Ruff check/format、MyPy `118 source files`、compileall、CLI help/OpenAPI/generated-schema drift 与 `git diff --check`：PASS；
- `#118` retention 测试覆盖 90/365 天边界、当前/上一 distinct stable pin、active canary/release chain、stale rejected 清理、rollback receipt、worker-only 权限、每表 bounded batch、cold-lane 10 秒 deadline 与错误隔离。

生产数据库仍为 `0283`。只读 preflight 还确认旧卷没有
`tracefold_review` 登录，且本机 `postgres_review_password` 路径是旧 bind
mount 留下的目录而不是密钥文件；代码现会 fail closed，并提供停库后
`make db-provision-review-role` 的一次性引导。上面所有验证均没有迁移
生产、修复该本机路径、写 review、切 shadow/canary 或改变 stable pointer。

## 12. 完成定义

只有下面一句话能关闭 #112：

> 在固定 Git/image/schema/Agent hashes 下，真实 accepted reviews 形成 development 集；一个只改 Prompt 的 DRAM candidate 在 candidate-unseen temporal holdout 上达到预注册 primary interval，且无 critical regression；随后完成 24h shadow、24h 10% canary 和可验证 promotion/rollback，所有 content-addressed receipts 已回填 Issue。

在那之前，最准确的项目状态是：**代码机制可上线取证，生产学习闭环尚未完成第一次闭环。**
