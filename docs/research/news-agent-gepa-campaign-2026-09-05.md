# News Agent 分析链路与 GEPA 战役：现状、对照与结论（2026-09-05）

写给没有读过代码的人。目标是用一页讲清三件事：这条新闻链路现在长什么样、最近两周我们改了什么并且改出了什么数字、官方 DSPy/GEPA 的做法与效果和我们的差在哪里。所有数字来自 GitHub Issue 上的回执（#504、#509、#522、#523、#534、#544、#548）与官方来源（GEPA 论文 arXiv 2507.19457 v2、dspy.ai 文档与教程），文末列出出处。

结论先行：

1. 链路本身已经从“推送洪水、重点档为零”调整到“每天约 470 张卡、重点档 7 张”，仍差目标（300–500 张、50–60 张重点）的重点档一截。
2. 我们第一次按官方规范跑通了完整的 GEPA 闭环：盲标 Gold 510 条 → 冻结 → 优化 → 注册 → 24 小时后的 held-out 测试。第一轮“赢了”的候选在 held-out 上被证明是过拟合，第二轮在更大的数据上种子本身就是最优。
3. 在当前 codebook、当前标准答案的一致性和 DeepSeek 老师的条件下，GEPA 已没有可兑现的提升；下一步是把 reviewer 的裁决规则写进 codebook（#567），而不是继续烧算力。

---

## 一、这个系统是什么

Tracefold 是一个 Python 服务加一个网页控制台。它做两件事：News（把新闻变成交易者要看的卡片）和 Trading（OI 信号与下单）。两者互不引用，只在 `tracefold.app` 拼装。本文只讲 News。

一个铁律贯穿全部设计：**PostgreSQL 里的事实是唯一的真相**。模型说了什么、缓存里有什么、页面显示什么，都不算真相。每一次“模型判断”、“送达”、“人工复盘”都是一条可追溯的行。

## 二、新闻分析链路：一条流水线上的八个工位

把它想象成工厂流水线，每个工位只做一件事，做完把结果写进数据库再交给下一个。

| 工位 | 做什么 | 关键事实 |
| --- | --- | --- |
| 1 抓取 | 从供应商（OpenNews 等）拉新闻，写入 `news_items` | 一条原文一行 |
| 2 准入与 Gate | 判断这条是否是新事件、属于哪个 storyline（故事线）、要不要进模型 | storyline 键来自代码注册表（#509），不再是正则 |
| 3 Program（三个模型步骤） | DSPy 程序按顺序跑三个 Predictor：EventSemantics（这是什么事、方向、幅度）、Taxonomy（四轴分类）、ReaderCard（中文标题与理由） | 本地 `qwen3.8-27b`，DeepSeek 兜底；三步各自的提示词是可版本化的“指令” |
| 4 decide() | 用代码规则决定 push / escalate / throttled / drop | policy v13：每 storyline 每小时预算、escalate 必须有来源权威佐证、营销类 listing 让路 |
| 5 送达 | 发到 Feishu，一次尝试，成败记账 | `news_deliveries.state = sent` 才算读者看到 |
| 6 ReviewDesk | 人或被授权的 AI 复盘：应不应该推、事实对不对、四轴分类的标准答案 | 标准答案叫 Gold，只有“显式接受”那一步才成为 Gold |
| 7 学习面 | 把 Gold 冻结成数据集、检查是否够用、跑 GEPA 优化、评估候选、决定晋升 | 全部是离线的手动工具，不在后台循环里 |
| 8 身份与 epoch | 三段指令 + 执行信封 + 模型槽位 + 策略版本一起哈希成 bundle；bundle 一变，就开一个新的 epoch | 只有当前 epoch 里、绑定当前 bundle 的 Gold 才能用于优化和评估 |

第 3 工位的四轴分类（`news_taxonomy_v1`）是本文的主角：

- `subject_codes`：这条新闻属于哪些 IPTC 主题（0 到 3 个码）
- `event_family`：发生了什么类型的事（13 类，如 macro_policy_data、product_service_change）
- `change_state`：事情处于什么状态（announced / scheduled / effective / reported / updated / unknown 等）
- `assertion_status`：证据可信到什么程度（confirmed / claimed / rumor / conflicted / unknown）

另外有一个代码算出来的 `source_authority`（来源权威），它不是模型判断，但 decide() 用它决定 escalate 是否有佐证。

一句话概括第 8 工位为什么重要：**改一次提示词或 codebook，就等于换了一个学生，之前给旧学生批的卷子不能再当他的成绩。** 这两周里 epoch 开了五次（#501、#504 seed、#522 各一次等），每次 Gold 归零，这是后面所有排期的约束。

## 三、最近两周改了什么

### 3.1 推送链路（#504 → #509 → #522 → #523）

| 问题 | 根因 | 改法 | 结果 |
| --- | --- | --- | --- |
| 每天 1174 张推送，62 % 集中在两个故事线 | decide() 只是模型自洽检查；进展定义让每次新打击都算新事实；storyline 全落到 `macro:general` | lexicon v3 + policy v12 每故事线预算 + seed 产品定义（#504） | 24 h 回执：push 469 + escalate 7，storyline 每小时 p95 从 13–17 降到 3.0 |
| storyline 键靠正则，28 % 落入 general | 正则无层级、无别名 | 代码注册表：actor / geo / conflict / topic 四类，389 个别名，Gate 词典并入（#509） | `none` 键占推送 10.5 %（目标 < 15 %） |
| 重点档（escalate）为零 | 来源权威注册表只覆盖 7 % 来源，D3 佐证规则把所有 escalate 降级；seed 自相矛盾；ReaderCard 出现空理由和 60 字硬截断 | 注册表补域名并按注册域后缀匹配、seed 措辞、ReaderCard 校验（#522） | 来源 unknown 占比 92.8 % → 74.1 %；escalate 从 0 到 12 小时 6 张 |
| 营销类 listing 被客观推送；方向反转被预算拦下 | listing 分支在 reader_value 之前；反转豁免只比最新一张 | policy v13 两处两行改动（#523） | 回放：拦 13 张营销帧、零真实上币损失；放行 5 张真反转 |

目标是每天 300–500 张、其中 50–60 张重点。现在张数达标，重点档还差 43–53 张，主要卡在来源权威的覆盖面。

### 3.2 顺手根除的生产缺陷

- **#544**：交易所时钟比主机快两百多毫秒，一条 OI 帧就让 workers 进程崩溃循环（6 小时 7 次）。根因是一条错误的数据库 CHECK。按 KISS 直接删掉两条“跨时钟”约束和一个丢帧守卫，不打补丁。
- **视图丢行**：事件的证据版本升到 v2 但判定还是 v1 时，整条事件从 review 视图消失，#534 因此丢了 4 条 Gold。视图改为按“判定所判的那一版”连接（#550）。
- **接受时不重算**：reviewer 改了分类标签后，`accept-drafts` 原样复制起草时的对错标记。现在按持久化的 Stable 标签重算（#550）。
- **评估器逐条汇总**：readiness 和 evaluator 把同一事实簇里每条的 Gold 都拿去汇总，同簇标签不同就 fail-closed，而 freeze 早就按“一簇一票”做了。两处都改成同一套簇代表选举（#545、#561）。
- **retention 地板算错对象**：`taxonomy_*` 四个对错标记是“优化目标自己的记分板”，却被当成 rubric 缺陷计入 boundary，使“Stable 全对”的簇永远不够 100。排除它们（#542）。

### 3.3 GEPA 战役（#534 → #548）

**Gold 怎么来的。** 每条待复盘的新闻，两个起草者在“盲”的条件下各给一份四轴标签（只看事件与 Gate 事实，看不到卡片、看不到 Stable 的答案），rubric 由第三个调用起草。两者一致就当草稿；不一致由 reviewer 读原文按 codebook 裁决。最后 reviewer 显式接受的才是 Gold。

- 起草者：`deepseek-v4-pro` + 本地 `qwen3.8-27b:thinking`（#534 决定：只用本机已有路由，不引入 MiniMax）。rubric 起草者从 qwen thinking 换成 DeepSeek，因为冒烟时前者 35 % 的草稿不合 schema，后者 0 %。
- 数量：开发集 313 条（126 + 200 个 task），held-out 197 条，共 510 条接受，1020 行 review。
- 速度：起草约 21 秒一条（三次模型调用），裁决一批 100 条约 40 分钟。
- 一致性：两个起草者四轴全同只有 19 %–43 %；按轴看 family 0.82–0.85、state 与 assertion 0.73–0.77、subject 只有 0.41–0.49。冻结时的 Cohen κ（全 epoch 442 簇）：family 0.803、assertion 0.652、change_state 0.639，subject F1 0.726。

**GEPA 是什么。** 把它想成“改作文的老师”：学生（本地 qwen）先做训练集的题；老师（DeepSeek）看错题和反馈，改写一版“答题指南”（就是 Taxonomy 那段指令）；再让学生做；反复几轮，保留在另一批题（selection 集）上均值最高的指南。官方的核心规则有三条：训练集用来反思、selection 集用来选冠军、**另留一批谁都没见过的题做期末考**。

**两轮结果。**

| 轮 | 数据 | 预算 | selection 结果 | 期末考 |
| --- | ---: | --- | --- | --- |
| 1 | 268 簇，selection 80 | light，9 次反思，748 次打分，23 分钟 | ADVANCE：72.6 → 77.0（+4.3） | offline 268 簇代表：+1.1 综合，但 change_state −1.9、subject −1.4、四轴全对 −0.7，只有 assertion +7.1 → **fail** |
| 2 | 442 簇（全部 510 条 Gold），selection 133 | medium，9 次反思，1438 次打分，41 分钟 | **NO_OP**：种子 82.3 分是 10 个候选里最高 | 未进入 |

第一轮的 +4.3 是对 80 道选择题的过拟合：它把 assertion 的分数买回来，代价是 change_state 和 subject。第二轮把选择集扩到 133 道，种子本身就赢，唯一接近的候选只是把同一笔交换反过来做。

## 四、和官方 DSPy / GEPA 对照

| 项 | 官方（已核对来源） | 我们 |
| --- | --- | --- |
| 训练 / 选择 / 测试三分 | 论文与教程都三分；`compile` 按 valset 均值选冠军；“keep a final test set separate” | 按事实簇的时间顺序 70/30 切训练与选择；期末考 = 注册后 24 小时的新窗口再盲标一批 Gold |
| 反思模型 | “benefits from a strong reflection model”；教程用 gpt-5 | `deepseek-v4-pro`（用户设定的上限） |
| 预算档 | light / medium / heavy = 6 / 12 / 18 个候选 | 第一轮 light，第二轮 medium |
| 反思小批量 | 默认 3 | 6（#501 决定，避免三张平局） |
| 候选选择 | Pareto 前沿负责探索，均值负责选择 | 同官方；准入再加“严格高于种子” |
| 论文效果 | Qwen3 8B 六任务平均 45.2 → 54.9；GPT-4.1 mini 53.0 → 65.2（测试集） | 综合 78.8 → 79.9（期末考，被拒）；第二轮 0 |
| 教程效果 | AIME 46.7 → 56.7；工单分类 75.4 → 87.0；PAPILLON 76.5 → 86.1（测试集） | 同上 |
| 起点 | 多在 38–47 分 | 72.6–82.3 分 |

为什么我们涨不动，按可信度排序：

1. **标准答案本身有噪音。** 两位起草者对 state / assertion 的 κ 只有 0.64–0.65，四轴全同不到一半。答案自己都不统一，指南写得再好也过不了这个天花板。官方任务的标签来自现成基准数据集，没有这个问题。
2. **起点高、任务碎。** 四个轴各自打分再平均，一条指南很容易在一个轴上得、另一个轴上失。官方任务是单一指标。
3. **老师弱、轮数少。** DeepSeek 对 gpt-5，9 次反思对官方 21–39 轮。
4. **每轮都会拿到“看起来的提升”。** 80 道选择题上 +4.3 很容易，这正是官方坚持期末考的理由；我们的评估器现在能把它挡下。

官方也不是回回赢：论文里 Qwen3 8B 做 AIME 时 GEPA 32 分，输给强化学习的 38 分。

## 五、现在的系统状态

- 运行中的 Program：`news_semantic_program_v9`，program sha `1ba5a6d9…`，policy v13，gate v6，epoch `bundle_dfd2e810`（2026-09-03 10:45 UTC 起）。
- 学习面：`news_reviews` 1020 行（510 条接受）；三个冻结数据集（开发 `bf77a4ea…` 268 簇、验证 `d27f0d97…` 176 簇、开发 `c8566ca5…` 442 簇）；一个已注册并被拒的候选 `8f237698…`，其 offline 评估报告与 release evidence 已落库。
- 运营方法：主机 CLI 连不上库，所有学习命令在 workers 容器或“一次性容器”（用单独标签的镜像 `docker compose run`）里跑；部署带迁移时需要 Nautilus 运行时先停后起；每次 `make up` 前用 `active_arm_manifest().bundle_sha` 核对身份未变。
- 与其他会话的协作：Trading 侧本周部署了 #556–#560，每次部署前核对身份、部署后我复核学习面，epoch 未动。

## 六、下一步（#567）

把两轮 reviewer 反复使用的 12 条规则写进 codebook（主题码正反例、change_state 与 assertion 的 precedence），这会开一个新 epoch，旧 Gold 留作审计，再按同样的流水线做一轮 Gold（≥ 250 簇）和一次 `--auto medium`。这是唯一同时提高“标准答案一致性”和“指南质量”的杠杆，也是官方“metric 是反馈通道”这条原则在我们这里的落点。更强的老师超出目前的边界，由用户决定。

## 七、小词典

- **Gold**：被显式接受的复盘答案，是训练与评估的唯一真值。
- **簇（cluster）**：同一件事的多篇报道连成一个簇；统计、抽样、投票都按簇，防止一篇新闻的十个转载把权重放大十倍。
- **selection 集**：GEPA 用来挑冠军的那 30 %；它见过冠军的分数，所以不是期末考。
- **held-out / holdout**：注册候选之后才出现的新数据，谁都没见过。
- **epoch / bundle**：一套指令、信封、模型槽位与策略的哈希；变一个字节就换一个 epoch。
- **κ（Cohen's kappa）**：两个标注者扣除随机一致后的一致程度，1 是完全一致，0.6 左右属于“勉强可用”。
- **NO_OP / ADVANCE / fail**：GEPA 没找到更好的指南 / 找到了 / 评估器判定候选不合格。

## 出处

- 仓库回执：Issue #504（推送评估与 24 h 回执）、#509（注册表）、#522 / #523（来源权威、policy v13）、#534（盲标、批次 1/2、冻结、run）、#544（时钟 CHECK）、#548（PR-A、PR-B、held-out 1/2、offline、第二轮）、#567（第三轮）。
- 官方：arXiv 2507.19457 v2（六任务表 1/2；“up to 35× fewer rollouts”）；dspy.ai/getting-started/gepa-optimization（test 集分离原文）；dspy.ai/diving-deeper/gepa-in-depth（6/12/18 候选；“frontier is for exploration; the aggregate is for selection”；强反思模型）；教程 gepa_aime、gepa_facilitysupportanalyzer、gepa_papillon。
