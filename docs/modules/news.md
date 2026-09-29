# News：增量新闻理解与独立通知

[手册](../README.md) · [系统架构](../ARCHITECTURE.md) · [OI](oi.md) · [交易研究](trading.md) · [排障](../OPERATIONS.md#news-retry)

News 不只是“把标题交给模型打分”。当前编辑型链路以 **来源修订 → 冻结输入 → 命题理解 → EventUpdate 采用 → 独立通知** 为主线。它分别保存来源说了什么、系统理解了什么、读者实际收到什么。

| 模块速览 | 说明 |
| :--- | :--- |
| **定位** | 信息产品 / 编辑型新闻 |
| **运行位置** | Workers · news-semantic 与独立通知续接 |
| **输入 → 产物** | 来源 Item、修订、冻结证据与 prior claims → EventUpdate、逐命题通知计划、实际回执、公开 outbox |

> [!IMPORTANT]
> 理解、采用和通知分别完成。通知失败不回滚知识；卡片也不是 Trading 的事实来源。

| 所有者 | 主要职责 |
| --- | --- |
| [receiver.py](../../tracefold/news/pipeline/receiver.py)、[recovery.py](../../tracefold/news/pipeline/recovery.py) | 接收 OpenNews，记录中断与有界恢复，将原始输入交给 broker |
| [admission.py](../../tracefold/news/pipeline/admission.py) | 区分来源契约，保存 Item、确定性拆分和 Event 归组，提交证据与语义工作 |
| [events](../../tracefold/news/events/) | FactUnit 范围、grounding、准入、身份、标题 / token / MinHash 候选匹配 |
| [semantic.py](../../tracefold/news/pipeline/semantic.py) | 消费语义唤醒，领取版本工作、执行尝试、退避、熔断与失败结算 |
| [updates/service.py](../../tracefold/news/updates/service.py) | `NewsAgent` 编排、采用与可选补读；`Notifications` 独立续接 |
| [semantics.py](../../tracefold/news/updates/semantics.py)、[judgment.py](../../tracefold/news/updates/judgment.py) | 引文校验、命题比较、有限问题、内容组装 |
| [dspy_backend.py](../../tracefold/news/updates/dspy_backend.py) | DSPy 抽取、中文文案、生成式判断与原生有限选项判断 |
| [notification.py](../../tracefold/news/updates/notification.py)、[reader_judgments.py](../../tracefold/news/updates/reader_judgments.py) | 逐命题决定表、读者新颖度、锚点与增量重要性判断、稳定意图与冻结卡片 |
| [event_update_store.py](../../tracefold/news/storage/event_update_store.py)、[event_updates.py](../../tracefold/news/storage/event_updates.py) | 短事务、检查点、不可变更新、head 条件采用、计划和发送账本 |
| [public.py](../../tracefold/news/updates/public.py) | 从已采用知识生成公开更新，不依赖读者卡片 |
| [delivery.py](../../tracefold/news/pipeline/delivery.py)、[maintenance.py](../../tracefold/news/pipeline/maintenance.py) | 通知轮询、真实投递、补唤醒与有界保留清理 |

[通知判断](#notification) · [输入身份](#input) · [Agent](#agent) · [状态恢复](#state)

<details>
<summary><strong>本页目录</strong></summary>

1. [端到端数据流](#section-端到端数据流)
2. [一条 News 为什么可能对应多个 Event](#section-一条-news-为什么可能对应多个-event)
3. [NewsAgent 到底做了什么](#section-newsagent-到底做了什么)
4. [主题、来源与知识版本](#section-主题来源与知识版本)
5. [状态必须分三层理解](#section-状态必须分三层理解)
6. [什么决定一条新闻是否推送](#section-什么决定一条新闻是否推送)
7. [一个具体更新例子](#section-一个具体更新例子)
8. [验证与排障入口](#section-验证与排障入口)
9. [源码责任地图](#section-源码责任地图)
10. [常见误解](#section-常见误解)

</details>

<a id="section-端到端数据流"></a>
## 01 · 端到端数据流

```mermaid
---
config:
  fontFamily: "system-ui, Noto Sans CJK SC, Microsoft YaHei, WenQuanYi Zen Hei, sans-serif"
  flowchart:
    curve: linear
    nodeSpacing: 28
    rankSpacing: 42
---
flowchart TB
    accTitle: 编辑型 News 端到端链路
    accDescr: 来源契约区分类型化市场观察与编辑型消息。编辑型消息持久保存证据和语义工作，经消息唤醒后增量采用知识，再独立续接通知与 Trading 公开交接。
    Source["OpenNews 原始记录"] --> Queue[("RabbitMQ · news.raw")]
    Queue --> Contract{"来源契约"}
    Contract -->|市场报告| Market["类型化市场观察<br/>独立解析与通知"]
    Contract -->|编辑型消息| Admit["修订、范围与归组<br/>保存 Item / Event / 证据"]
    Admit --> Work[("PostgreSQL · 语义工作<br/>wanted revision / lease")]
    Work -->|news.triage 唤醒| Agent["NewsAgent<br/>增量抽取、判断与条件采用"]
    Agent --> Update[("EventUpdate / head")]
    Update --> Public["公开 outbox<br/>独立 Trading 交接"]
    Update --> Notify["通知计划与卡片<br/>实际正文与发送回执"]

    classDef news fill:#ecfdf5,stroke:#0f766e,color:#134e4a,stroke-width:1.5px
    classDef research fill:#eef2ff,stroke:#6366f1,color:#312e81,stroke-width:1.5px
    classDef execution fill:#fff7ed,stroke:#c2410c,color:#7c2d12,stroke-width:1.5px
    classDef store fill:#f1f5f9,stroke:#64748b,color:#1e293b,stroke-width:1.5px
    classDef external fill:#f8fafc,stroke:#94a3b8,color:#334155,stroke-dasharray:4 3
class Source external;
class Queue,Work,Update store;
class Contract,Market,Admit,Agent,Public,Notify news;
```

*数据流 · 圆柱表示持久队列或账本；采用、公开转交和发送是不同边界。市场分支不经过编辑型 Agent。*

PostgreSQL 保存可恢复工作；RabbitMQ 的语义消息只是唤醒。准入事务先提交，再发布唤醒并记录发布状态；进程在这几步之间崩溃，由维护任务重新唤醒。它不是 PostgreSQL 与 RabbitMQ 共享一个事务。

<a id="input"></a>
<a id="section-一条-news-为什么可能对应多个-event"></a>
## 02 · 一条 News 为什么可能对应多个 Event

### Item、FactUnit、Event 与 Claim

| 对象 | 含义 | 不能混淆的身份 |
| --- | --- | --- |
| Item | 一条提供商记录；保留原始内容与来源信息 | 不等于某一条模型命题 |
| Item revision | 同一记录后续观察到的正文版本，带修订顺序和前驱 | 不等于正文哈希；A → B → A 是三个版本出现 |
| FactUnit | 从一个明确编号汇总中切出的独立输入范围 | 不等于“每句话都拆成一个 Event” |
| Event | 准入层维护的一组相关来源证据 | 不保证只包含一个 Claim，也不是全局故事百科 |
| Claim | 带字段、资产角色、原文引文与稳定引用的命题 | 不等于标题、卡片或一个 ticker |
| EventUpdate | 一次已采用的知识版本，包含命题、关系、变更与问题 | 不等于一次模型请求或一次推送 |

[extract_fact_units](../../tracefold/news/events/facts.py)只拆**至少三个连续、显式编号块**的高置信度汇总。编号后的连续段落保留在该 FactUnit 范围内，列表前后的公共限定保留为共享上下文；事实身份仍由编号锚决定，不随续段增长而改变。不满足该结构时保持一个整体输入，其事实身份只由记录决定：同一记录改了标题是同一事实的新版本，不另开 Event。时间、金额和一般段落不应被当作编号列表拆分。

因此，“一条提供商消息 → 多个 FactUnit → 多个 Event”在当前实现中是有条件的；与此同时，**一个 EventUpdate 本身可以包含多条 Claim**。系统没有让 LLM 自由把每句话扩张成一个独立 Event。

更新已有消息时，系统按既有范围及修订关系读取证据，不把旧正文的字符偏移硬套在新正文上。[唯一阅读投影](../../tracefold/news/updates/projection.py)按本轮来源版本定位任务片段；定位不唯一时展示完整来源。模型只收到该视图，完整 Evidence 仍保存。引用必须同时出现在本轮一个连续可见片段和对应冻结来源中。

编号列表的最后一条任务与其后连续正文构成一个可引用片段；若该任务不是当前 Event 的事实，后文仍只作为共享上下文。更正历史归属时，`scope_retraction` 在新的 EventUpdate 中退休误归属 Claim，并向 Trading 发布 `source_update`；修复证明独立记在 `news_head_scope_repairs`，不伪造模型观察，也不改写旧 EventUpdate、来源或发送回执。修复与语义采用共用 Event 锁及提交路径；按 Event/head/proof 有界执行。纯修复不重新打开已完成的通知工作，也不重置尝试数；仍 pending 或已 `failed` 的工作转向修复后 head，失败工作之后按新 head 定向重试。精确命令见[运维指南](../OPERATIONS.md#历史编号事实的-head-归属清理)。

### 命题身份不随每次关系变化重建

Claim ref 指向一个命题或真实世界中的一次发生。新增支持、来源、跨 Event 冲突或更正关系可以改变 EventUpdate，但不必创建另一个 ref；组装时仍保留关系和证据增量。显式 A → B → A 的状态转换则保留不同发生身份，不能因为字段重新相等就吞掉最后一次动作。

对于同一 Event 中两路来源携带完全一致的被引全文，即使关系模型误判为 `unrelated`，只有命题 statement 相同、没有新增数量 / 发生转换，且结构化身份检查无冲突时，才允许窄范围复用旧 Claim。它不是只凭相似标题跨 Event 去重。

### 准入与候选归组不是语义裁决

[Gate](../../tracefold/news/events/gate.py)与[准入](../../tracefold/news/pipeline/admission.py)负责确定性证据、grounded assets 与队列优先级。来源标签、显式 cashtag、交易标的目录与规则匹配各有作用；并非所有实体都由一次模型调用凭空产生。

精确身份和有界近似匹配用于找到可能归到一起的输入。强事实如 ticker、数字及 token 兼容性可以阻止错误合并；MinHash、标题相似或同一个资产只能帮助召回，**不能证明两个命题相同**。真正的等价、补充、更正与阶段变化在 Claim 层判断。实时稿只按文本并入已准入的 Event：断线补抄开出的 recovery Event 不产生语义、卡片或 catalyst，不能吞掉之后的实时稿；补抄稿仍可并入它作为历史。

证据快照记录成员的来源、策略与 provenance，但只有语义材料变化（任务范围、成员记录与事实、正文修订、grounded assets）才请求语义工作；同一记录换策略重发只更新快照，不触发空转。

原始市场报告走单独的 `admit_market_item`：保存 Item 与解析结果，不创建伪 Event，不走编辑型 Gate / MinHash / 语义链路。

<a id="agent"></a>
<a id="section-newsagent-到底做了什么"></a>
## 03 · NewsAgent 到底做了什么

```mermaid
---
config:
  fontFamily: "system-ui, Noto Sans CJK SC, Microsoft YaHei, WenQuanYi Zen Hei, sans-serif"
  sequence:
    mirrorActors: false
    messageMargin: 28
    actorMargin: 40
    wrap: true
---
sequenceDiagram
    accTitle: NewsAgent 的增量采用
    accDescr: 领取版本与租约后读取检查点；只对新增材料抽取和理解，保存观察并按 head 条件采用；可选补读不撤销已提交结果。
    autonumber
    participant W as SemanticWorker
    participant D as NewsStore
    participant A as NewsAgent
    participant M as DSPy 适配器
    W->>D: 领取 wanted revision、owner token 和 lease
    W->>A: 传入冻结输入与尝试边界
    A->>D: 读取该工作身份的抽取检查点
    alt 抽取检查点不存在
        A->>M: 抽取新证据的结构化命题与引文，逐条校验
        A->>D: 保存抽取检查点
    end
    A->>M: 逐对判断关系与来源支撑；缓存命中的判断不再调用
    A->>D: 保存语义观察
    A->>D: 校验所有权与 head，原子采用知识版本
    D-->>A: 同时记录公开 outbox 与通知工作
    A->>D: 结算当前输入版本
    opt 有明确问题、允许目标且剩余预算足够
        A->>D: 保留一次补读名额并附加可用材料
    end
```

*时序 · 展示一次能够采用的正常尝试。缓存命中、无变化、重试与 head 冲突会改变实际调用数量。*

### 冻结输入与增量范围

冻结输入绑定 Event、输入修订、证据范围、prior claims、候选关系、来源时钟以及程序 / 模型身份。prior claims 只取**当前有效**命题（未被更正退休、未被真实变化替代），先本 Event，后召回的相关 Event；“当前有效”只由 `EventUpdate.current_claims` 一处推导。输入身份 `input_sha` 只含本 Event 的命题：相关 Event 再次采用不改变身份，重试复用已保存的抽取，只重问比较（答案按内容缓存）；相关 Event 的命题也不进入抽取输入。每个来源的当前 Event 阅读范围产生 `read_ref`，由任务边界、实际片段和投影版本决定。先构造阅读视图再比较已处理的 `processed_read_refs` 与已隔离的 `failed_read_refs`；同一来源新增范围或条件仍需处理，重投相同任务不重算。旧命题用于比较与延续，不是每次把全部历史成员重新抽取。

“读过但没有命题”的材料也必须记入任务级处理身份。否则同一段空内容会不断进入下一轮。没有新证据或没有实质变化，可以推进 done，而不制造新的内容版本；首个版本没有命题时不采纳 EventUpdate。以失败结束的修订把它读过的范围隔离，之后的新成员只读新材料。对已完成或已失败、确认需要重读的 Event，使用精确 wanted/head/read 身份的 `news reanalyze`；它不伪造来源修订或自动重发历史通知。

来源修订使用**本地观察顺序、修订序号和前驱**，不把提供商发布时间猜成可靠的编辑版本号。同一来源的新旧正文可以同时作为历史证据保存，但当前支撑判断只采用该来源的当前贡献，不能把转载或同源修订算成多个独立证实。

### 抽取、判断与采用各司其职

| 步骤 | 模型承担的工作 | 代码承担的工作 |
| --- | --- | --- |
| 抽取与理解 | 同次给出命题、条件、数量、资产角色、时间、引文、`mode`、`phase`、`content_kind`、逐命题主题与证据支撑提示；不比较旧命题 | 逐条检查结构与引文：不可用的读数（如 `300亿` 这类非十进制数量、未知 phase）只从该命题去掉，坏命题丢弃并记原因，其余照常；引文容忍大小写与空白差异，保存来源原文片段 |
| 比较 | 每条新命题与每条当前有效的旧命题逐对判断等价、补充、更正、替代、冲突，以及证据支撑关系；抽取不给关系提示 | 排除已能证明的数字 / 语气矛盾，验证目标 refs 与候选身份；核对更正 / 替代的时间先后；冲突只注释新命题，不吞掉其 catalyst；同一冲突只在首次建立时发布 |
| 组织 | 提议影响机制与未解问题 | 验证支持关系；区分事实与条件性推论；延续未被显式改变的知识 |
| 采用 | 不直接写数据库 | 组装 EventUpdate，保存观察，检查 owner / lease / head 后条件采用 |

当前独立判断任务包括 `relation`、`support`、`next_read`，并非每条消息都需要全部任务。通知侧的读者判断不在这张任务表里，修改它不会让语义检查点失效。关系始终逐对判断：#742 曾重放“先分诊、再细判”，在本地生成式模型上无法做到关系召回不降（通知侧读者新颖度依赖 `equivalent` / `adds_information` 链接，Trading 依赖更正与替代），且只省约 5% 调用，因此未采用。

可选原生判断通过 DSPy 的有限输出类型连接 Jev / System One。未配置该后端时使用生成式判断；已成功缓存的答案不再找另一个模型投票。一组问题的缓存读取是一条 SQL，每个批次的写入是一条 SQL；批次有界并行（至多 3 个），一个批次响应不可用只让它自己的问题不可得，不连累其他批次或整个修订。选项标签按大小写与分隔符规范化，无法识别的标签只让该条不可得。失败回退、批次与缓存身份由 [judgment.py](../../tracefold/news/updates/judgment.py)及 [DSPy 适配](../../tracefold/news/updates/dspy_backend.py)控制。

### 模型到底调用几次，为什么有延时

**不是固定三个 DSPy 节点，也不是一个 Event 只调用一次模型。** 一次语义尝试可能包含抽取、多个关系判断批次、缓存命中、回退，以及采用冲突后的缺失关系补算。Jev 抽取同时给出 mode、phase、content_kind 和每条命题的主题，不再对同一输入重复执行原生分类。通知阶段先按持久化的命题链接判断读者新颖度，再对规则未决定的每条命题发一次读者判断（锚点 + 增量重要性）；若有命题选中，再生成中文卡片。

| 预算 | 当前代码值 | 解释 |
| --- | --- | --- |
| 语义阶段 | 120 秒 | 一次 `NewsAgent.process` 的共享截止时间 |
| 通知模型阶段 | 60 秒 | 计划与卡片生成共享，不把外部发送等待算成同一次模型阶段 |
| 通知准备在途上限 | `news.push.notification_prepare_limit`，默认 2 | 只限制实际准备任务和就绪结果；进程唯一的发送时隙不占准备位置；快 Event 可先完成 |
| 生成调用预算常量 | 60 秒 | 具体适配使用的调用边界；不等于端到端保证 |
| 采用冲突尝试 | 2 次 | 处理 head 变化，不无条件重做已完成的抽取 |

数值来自 [service.py](../../tracefold/news/updates/service.py)。它们是上限，不是实际耗时、服务级别承诺或性能实测。排查延时需要拆开：**入队等待 → DB 领取 → 模型物理调用 → 判断 / 回退 → 采用 → 通知等待 → 发送**。把总耗时都称为“Agent 慢”无法定位根因。

提供商失败与内容不确定不同：非最终尝试中，关键关系 / 支撑判断无法取得会进入持久重试；最终尝试允许按契约保存 unresolved / `possible_new`，不能伪造“没有新闻价值”。程序错误和非法核心输出仍是失败。

生成输出具体区分 `news_generation_output_truncated`、`news_generation_output_empty` 与 `news_generation_output_schema_invalid`。已配置的 fallback 只有请求契约有实质差异时才可补答一次；固定契约或引用错误不再消耗相同请求的多轮语义重试。临时限流、超时、服务端和传输错误仍走有界恢复。错误日志记录错误类别与长度，不记录原始模型响应片段。

### 可选补读的边界

系统只能读冻结输入提供的目标，由实际 `ExistingSourceReader` 实现提供材料。它不是任意网页浏览器、shell 或自主搜索 Agent。一条 lineage 通过持久 reservation 限制一次补读，重试不能获得新名额；失败不能撤销已经提交的 EventUpdate、公开 outbox 或通知工作。

<a id="topics-and-cited-source-authority"></a>
<a id="section-主题来源与知识版本"></a>
## 04 · 主题、来源与知识版本

[topics.py](../../tracefold/news/updates/topics.py)维护 IPTC 导航主题，最多保留三个，不同时保留冗余父子主题。当前 v2 将主题归属到命题并从有效命题汇总，避免已失效内容长期污染 Event 标签。

[taxonomy.py](../../tracefold/news/taxonomy.py)保留来源权威类别，例如 `regulatory_filing`、`issuer_first_party`、`reputable_secondary` 与 `unknown`，依据已识别的来源身份。来源权威不是对其引用的第三方说法进行独立核验，更不是交易指令。

当前 EventUpdate 使用 `news_event_update_v2`；0411 已清除退役 v1 所属 Event、旧 verdict / Review / 学习表。当前 ReviewDesk 处理通知决策反馈及外部漏报，不把旧四轴 taxonomy 或旧 Program 的 `fact_kind` 当作新 Claim 契约。

<a id="state"></a>
<a id="5-work-progress-and-recovery"></a>
<a id="section-状态必须分三层理解"></a>
## 05 · 状态必须分三层理解

| 层次 | 记录什么 | 典型情况 |
| --- | --- | --- |
| 语义工作 | wanted / done 输入版本、owner、lease、attempt、due 和错误 | 最新输入待处理、暂缓或失败；最后一次尝试仍可能合法运行 |
| 已采用知识 | 不可变 EventUpdate、content revision、当前 head | 无实质变化不增加 head；失败不删除上一有效版本 |
| 读者结果 | 通知工作、逐命题计划、intent、冻结文案与发送账本 | 不通知、生成失败、等待发送、已发送或结果不明 |

下面是**恢复过程的概念状态图**，不是完整数据库枚举：

```mermaid
---
config:
  fontFamily: "system-ui, Noto Sans CJK SC, Microsoft YaHei, WenQuanYi Zen Hei, sans-serif"
---
stateDiagram-v2
    accTitle: 语义工作的恢复状态
    accDescr: 新证据产生待处理工作，领取租约后可采用、无变化或失败；可恢复错误及精确版本恢复重新进入待处理。
    [*] --> Pending: 新证据提交工作
    Pending --> Owned: 成功领取版本与租约
    Owned --> Adopted: 采用有实质变化的知识
    Owned --> Unchanged: 无新增实质变化
    Owned --> Pending: 可恢复失败且预算剩余
    Owned --> Failed: 已结算失败或最终租约过期
    Adopted --> [*]
    Unchanged --> [*]
    Failed --> Pending: 新证据、精确版本重读或人工恢复
```

*概念状态 · 标签帮助理解工作结果，不是新增 Event.status，也不把“采用”当成“已发送”。*

`/api/news/status` 分别报告可领取的 `semantic_pending`、等待调度的 `semantic_deferred`、持有有效租约的 `semantic_in_progress` 和终结的 `semantic_failed_exhausted`。失败工作不再计为可运行 pending，并保留真实尝试次数（一次性的契约错误不伪装成用尽三次）；这些是有界工作集的状态计数，不是模型调用数。控制台把失败显示为“解析失败”并附中文原因与错误码；仍有失败待处理时模型健康至少为 warn。

以失败结束的修订（含 Janitor 结算的崩溃最终尝试）只隔离该次尝试实际送入的任务范围，之后的修订不再重复送入；尝试开始后才加入的新成员不受影响，照常抽取与采用。构建冻结输入本身失败时只让该 Event 的工作失败，不让语义消费者故障。

采用只有带新通知义务的变化（`new_fact`、`possible_new`、`parameter_change`、`phase_change`、`scope_change`、`correction`、`conflict`）才新建或重置通知工作；只新增证据、复述或空更新不建工作，但尚未完成的通知工作照常转向最新 head，保留已用尝试。

最终尝试仍持有有效 lease 时，Janitor 不能将它判为耗尽；只有崩溃且 lease 已过期的最后尝试才应被终结。旧版本的延后 / 失败结算不能消耗新版本预算，也不能推迟后继工作。

没有模型配置时，Worker 可以确认 broker 唤醒但保留 PostgreSQL 中待处理工作；这不代表已完成理解。更换模型、提示词或镜像不会自动重放所有已处理证据，也不自动重置耗尽预算。

<a id="notification"></a>
<a id="section-什么决定一条新闻是否推送"></a>
## 06 · 什么决定一条新闻是否推送

决定者是 **`NotificationPlanner.plan`** 调用的纯函数 **`decide()`**，输入是已采用 EventUpdate、持久化的命题链接、读者实际收到的回执及未决发送状态。它为**每条 Claim**按固定顺序给出 `notify` / `not_notified` / `deferred` 与具名原因；模型只提供分布，切点、规则顺序和重点都在代码里。

| 顺序 | 情况 | 原因与行为 |
| --- | --- | --- |
| 1 | 命题已退休、替代或被跨 Event 修订失效 | `retired`，不通知 |
| 2 | 本 Event 的发送仍在进行（`sending`） | `send_outcome_unresolved`，暂缓；等待不计尝试 |
| 3 | 本 Event 的发送结果不明（`ambiguous`） | `send_outcome_ambiguous`，按可能已送达处理：不重发，也不阻塞其他命题 |
| 4 | 首次可见超过 3 小时（已送内容的更正 12 小时） | `stale_source`，不通知 |
| 5 | 命题所述事件（`occurred_at`）早于首次可见超过 7 天，如旧闻汇总、背景段落；更正和带 `speaker` 的表态不适用 | `stale_occurrence`，不通知 |
| 6 | 新颖度 known：链接到读者已收到的命题（等价，或已送命题是更全 / 更新的版本） | `known_to_reader`，不通知 |
| 7 | 新颖度 in_flight：链接到的命题正在发送 | `linked_send_in_flight`，暂缓 |
| 8 | 更正已送命题（`corrects`，且本命题首次可见晚于那次送达） | `correction_of_sent`，推送，卡片注明更正此前哪条 |
| 9 | 上币公告 | `protected_listing`，推送 |
| 10 | 商品 / 指数的当日价格变动（`level_crossed`、数量是变动而非水平、周期为当日）≥ 5% | `large_daily_move`，推送 |
| 11 | 其余命题：一次读者判断，按作答后端的切点 | 增量重要性 ≥ KEY_CUT 为 `reader_key`（推送并标重点）；≥ PUSH_CUT 为 `reader_push`；否则 `reader_feed` 只进信息流。读者已有核心事实的命题（链接为已送命题的 increment，或未链接但锚点指向已送消息）要 ≥ KEY_CUT 才推送 |
| 12 | 读者判断暂不可得 | `reader_unavailable` 暂缓；采纳 10 分钟后仍不可得记为 `reader_unassessed`，不推送 |

**事件时间**也由代码判断（[notification.py](../../tracefold/news/updates/notification.py) 的 `stale_occurrence`），读法保守，因为抽取会编造年份：
- ISO 日期：引文写出了年份才用该年份，否则取离首次可见最近的年份。
- 单独的月份（可带 early / mid- / late）：取该月最后一天，默认在过去一年内；只对事件类命题（`state_change`、`official_measure`、`other`）成立，数字类命题里的月份是统计期。
- 带 `speaker` 的命题本身就是新表态，不判断；其余写法（如 “Sept. 10”）也不判断。

**读者新颖度**是纯代码：采纳事务把每个修订 `changes` 里带 `previous_ref` 的比较写入只追加的 `news_claim_links`；通知快照在短事务里从两端读取链接（至多两跳，两跳须经过 `equivalent`），与已送 / 结果不明 / 发送中回执的 `claim_refs` 求交，得到 known / increment / development / in_flight / unlinked。同一对命题以最新一次断言为准，某个修订不再提及不算撤回；因此链接不会因后续修订的 head 不再重复而丢失。

**读者判断**是一次请求两道题（[reader_judgments.py](../../tracefold/news/updates/reader_judgments.py)），每条命题有自己的冻结 `ReaderInput`：命题字段与可读主题、来源，以及至多 16 条实际已送正文。输入不含未指向某条已送消息的单个 `change` 类型；`EventUpdate.changes` 和持久命题链接仍决定更正、增量与新颖度。

回执召回（[receipt_recall.py](../../tracefold/news/updates/receipt_recall.py)）从已送 `news_deliveries` 出发，普通窗口按 `settled_at_ms` 连续覆盖过去 48 小时。每条当前命题独立查询历史已送版本 `(event_id, content_revision)` 中该回执的 `claim_refs`：有效语义代表优先，结构身份 / 主资产与同语言实义词项两路各取最多 32 个候选，确定性融合后可返回 0–16 条。资产按规范符号比较并保留 `market_type` 与 primary / mentioned 角色：去掉 `$` 与 venue 前缀，经品种目录的种子别名（`XAU`→`GOLD`、`XAG`→`SILVER`、`WTI`→`CL`）解析；商品资产还按 Gate 接地表的中英文名称识别（`现货白银`→`SILVER`），SQL 路线与纯函数用同一张别名表和同一组模式。英文词项使用 PostgreSQL `ts_rank_cd`，停用词只含英文功能词；中文使用对称 CJK bigram；二者不提供无共同实体的通用跨语言语义匹配。共享 SQL 批次和正文缓存，但兄弟命题不共享截断后的列表。缺少历史投影时只可使用真实已送正文的合法词项路径，不借当前 head 补造历史。

同一 reader context 同时生成模型正文与 revision，由快照、记录计划、开始发送前两处校验复用。正文必须与已送 payload digest 一致；`sending` 不当作已读，`ambiguous` 保留去重保护。召回依据仅选上下文，是否已覆盖仍由持久关系、新颖度和 reader 判断决定。等价比较只用可证明的身份、枚举、同口径数量及少数可解析绝对时段冲突否决模型的 equivalent；同口径数量指同一指标与单位（忽略大小写与首尾空白）、周期可对齐，不要求主体或对象文本一致，主体不同须由 `subject_id` / `object_id` 证明。自由文本差异返回未知，不代表已经证明等价。

- 锚点题（`Choice` m1…mN / none）：哪条已送消息已经报过本命题的核心事实（同一主体、动作、对象，允许本命题多出细节）。未链接的命题有锚点时，推送门槛提高到 KEY_CUT。卡片的“补充”写法也看锚点：进展（development）对所链接的已送命题写“补充”；其余命题只有锚点指向某条已送消息时才写“补充”，并引用那条消息。链接为 increment 而锚点为 none 时按完整渲染，因为一条链接可能把同一故事里的不同事实连在一起。
- 增量重要性题（5 档 `Score`）：本命题相对已送消息新增的信息值不值得打断；没有已送消息时评价命题本身。完全重复自然落在低档。

原生判断走通知决策层独用的 `llm.news_reader_judgment`（System One）；不可用或超时则同一签名一次回退到生成式 News 路由，两者切点分别测定。答案按“判断器身份 + 冻结输入摘要”写入 `news_judgment_cache`，兄弟命题变化或 CAS 失败都不重问；不可用的答案不缓存。

每条决定记录新颖度、所用链接或锚点回执、渲染方式（完整 / 补充 / 更正）、分值分布与作答后端；控制台显示模板化原因，原因后面的 `×N` 统计具有该原因的命题数，不是报道数。`key` 是重点展示标记，不是另一轮发送审批或仓位权重。`editorial_v1` 历史决定按旧原因显示表只读展示。

离线重放使用 [eval_news_reader.py](../../scripts/eval_news_reader.py)：在 2026-09-28 归档的 `news_reader_input_v1` 输入与独立标注上，用已记录的回答经现行 `reader_decision` 与切点评分，钉住决策层质量线；它不调用模型，归档输入也不进入现行模型或缓存。修改切点或新颖度规则时重跑它；修改档位文本、指令或模型时，须在现行输入契约上重新提问评测（样本不进仓库），并把结果写进 PR。召回由 [回执召回测试](../../tests/news/test_news_receipt_recall.py) 与真实 PostgreSQL 上的黄金案例覆盖。#725 编辑器的有限对照见 [#725 对照报告](../reports/issue-725-attention-2026-09-27.md)。

### 选择：哪些命题需要通知

```mermaid
---
config:
  fontFamily: "system-ui, Noto Sans CJK SC, Microsoft YaHei, WenQuanYi Zen Hei, sans-serif"
  flowchart:
    curve: linear
    nodeSpacing: 28
    rankSpacing: 42
---
flowchart TB
    accTitle: 逐命题通知选择
    accDescr: 逐命题检查后，未决发送暂缓；命题链接给出读者新颖度；其余命题一次读者判断得到锚点与增量重要性，由代码切点决定；产物是具名不通知或明确选中集合。
    Snapshot["已采用知识 + 读者快照<br/>命题链接与相关回执"] --> Rules["失效、发送中、结果不明、时效"]
    Rules --> Overlap{"发送未决？"}
    Overlap -->|是| Defer["暂缓相关命题"]
    Overlap -->|否| Coverage["读者新颖度<br/>known / in_flight / 更正"]
    Coverage --> Editor["一次读者判断<br/>锚点 + 增量重要性 → 切点"]
    Editor --> Select{"有待通知命题？"}
    Select -->|否| Hold["具名不通知原因"]
    Select -->|是| Selected["选中命题与计划身份"]

    classDef news fill:#ecfdf5,stroke:#0f766e,color:#134e4a,stroke-width:1.5px
    classDef research fill:#eef2ff,stroke:#6366f1,color:#312e81,stroke-width:1.5px
    classDef execution fill:#fff7ed,stroke:#c2410c,color:#7c2d12,stroke-width:1.5px
    classDef store fill:#f1f5f9,stroke:#64748b,color:#1e293b,stroke-width:1.5px
    classDef external fill:#f8fafc,stroke:#94a3b8,color:#334155,stroke-dasharray:4 3
class Snapshot,Rules,Overlap,Defer,Coverage,Editor,Select,Hold,Selected news;
```

*决策视图 · 同一计划可以包含不同命题结果；选中仍不等于已发送。*

### 发送：从意图到实际结果

```mermaid
---
config:
  fontFamily: "system-ui, Noto Sans CJK SC, Microsoft YaHei, WenQuanYi Zen Hei, sans-serif"
  flowchart:
    curve: linear
    nodeSpacing: 28
    rankSpacing: 42
---
flowchart TB
    accTitle: 通知意图到实际发送
    accDescr: 由选中计划保留稳定 intent，生成并冻结文案。发送前复查 head、读者与所有权，不满足时重规划；满足时在事务外发送并保存实际回执。
    Plan["选中命题与计划身份"] --> Intent["保留稳定 intent"]
    Intent --> Copy["按真实输入复用或生成文案<br/>冻结正文与摘要"]
    Copy --> Slot["等待共用发送时隙<br/>目标检查与最终渲染"]
    Slot --> Check{"head、读者、所有权<br/>仍符合发送条件？"}
    Check -->|否| Replan["停止本次发送<br/>返回现有重规划路径"]
    Check -->|是| Send["事务外发送"]
    Send --> Ledger[("持久化精确正文与实际结果<br/>sent / not_sent / ambiguous")]
    Ledger --> Slot

    classDef news fill:#ecfdf5,stroke:#0f766e,color:#134e4a,stroke-width:1.5px
    classDef research fill:#eef2ff,stroke:#6366f1,color:#312e81,stroke-width:1.5px
    classDef execution fill:#fff7ed,stroke:#c2410c,color:#7c2d12,stroke-width:1.5px
    classDef store fill:#f1f5f9,stroke:#64748b,color:#1e293b,stroke-width:1.5px
    classDef external fill:#f8fafc,stroke:#94a3b8,color:#334155,stroke-dasharray:4 3
class Plan,Intent,Copy,Slot,Check,Replan,Send news;
class Ledger store;
```

*发送视图 · 复查不通过就停止本次发送；结果不明不能当作已证明未发送。*

### 正文、回执与重试

CardComposer 只收到选中 Claim 的 ref、statement、结构化 fields、精确引用和最少来源身份，而非完整来源全文或整个知识文档；按“补充”或“更正”渲染的命题另带读者已收到的那条消息正文。这段正文只说明哪些内容不必重复，不提供事实、名称、术语或数字，这些一律取自命题本身。“补充”文案以“补充：”开头，只写新增部分，不复述也不点名此前那条；“更正”文案以“更正：”开头，注明更正此前哪条。中文表达必须保留对象、动作、数量、归因与阶段；标题压缩也不能把“宣称”写成“核实”、把“宣布”写成“已经实施”。冻结卡片仍检查 refs 与形状；约束和脚本回归不证明真实模型每次都翻译正确。

“选中了某条 Claim”不证明卡片正文完整表达了它。后续读者判断读的是 `update` 回执的**实际发送正文**，不是来源全文、计划选择集合或某个抽象“已推送 Event”标记；发送正文与摘要必须来自可核验的冻结卡片。

计划采用和发送前会在 Event 锁下核对 head 与 reader revision。reader revision 只摘要**本 Event 的相关回执**（本 Event 已送、按文本相似召回的已送回执，以及命题链接两跳内可达命题的已送 / 结果不明 / 发送中回执，各取 intent 与状态），加上这些命题链接、本 Event 发送中 / 结果不明的命题、跨 Event 失效命题和观察名单；快照读到自身时间戳为止，两次 CAS 在短事务里读已结算的全部。无关 Event 的发送不再使计划或就绪卡片失效，相关的新发送必定使其失效。判断先于 reader 检查写入不可变决策与判断缓存，输家计划的读者判断下一轮直接复用；与上一轮完全相同的计划就是同一决策行，不追加记录。重新租约时 intent 改绑当前决策。

正文冻结后不因后台新材料到来而改写已开始的发送。适配器区分 `sent`、`not_sent`、`ambiguous`；目标预检只读，任何预检失败都是可重试的 `not_sent`，且不经过 `sending` 行；未进入提交边界的准入超时同样是 `not_sent`。只有已证明未发送且可重试的结果才按原意图重试。已知结果的结算使用同一结果重试，队列或账本保留 lease 与结果身份，即使发送回执后续补充也可验证重复结算；旧 lease 的迟到结果不能结算下一次发送。结果不明不能伪装成功，也不能直接再发一份。

通知准备保持有界并持续补位，最终发送由单个结算者串行执行。一个 Event 的准备失败（包括数据库暂时无法应答）只影响该 Event，不会取消另一个 Event 正在进行的发送；发送由自身持有到结算。停机时不再接新准备，已在途的发送有界等待结算后再退出，就绪但未发送的 intent 释放。进程启动时以及运行期间每 30 秒，把超过 60 秒且无本进程所有者的 `sending` 行对账为 `ambiguous`，并完成它所属的计划。

每次判断保存不可变决策输入与逐命题结果，通知工作和 intent 引用该决策。决策的 `plan` 同时记录比对过的已送回执（`compared_receipts`：intent 与正文摘要）和计时（`timings`：到期、开始、快照 / 读者判断耗时、完成时间），决策行 `created_at_ms` 即写入时间；发送账本的 `history_context.timings` 记录卡片开始 / 结束、就绪时间与等待发送时隙的时长。采纳 → 决定 → 发送的各段耗时因此可直接用 SQL 从库中取数。读者判断按冻结输入与判断器身份逐命题复用；文案按选中命题的完整表达材料（含“补充 / 更正”对照正文）和文案器身份复用；最终计划仍按当前 reader/head 重新检查。读者判断不可用时暂缓并记录错误码，超过上限记为未评估，从不按猜测推送；数据库、配置或外层期限故障不伪装成读者判断。

通知工作状态为 `pending` / `done` / `failed`。只有真实失败才计尝试：规划异常计入工作尝试；卡片失败与可重试 `not_sent` 计入 intent 尝试。第三次失败使工作进入 `failed` 并记录 `last_error_code`，不再自动领取；等待本 Event 在途发送、CAS 失败或数据库暂时无法应答都不计尝试。精确恢复命令及限制见[运维指南](../OPERATIONS.md#news-retry)；任何已有发送账本的 intent 都不能通过 `retry-work` 重开。

<a id="section-一个具体更新例子"></a>
## 07 · 一个具体更新例子

以下是**说明流程的虚构消息**，不是测试执行结果，也不承诺模型一定作出正确判断。

| 输入时刻 | 来源变化 | 应阅读的系统事实 |
| --- | --- | --- |
| T0 | 公司甲宣布工厂将在下月投产 | Claim 是“宣布未来行动”，不是“已经完成投产”；EventUpdate 保存引文和阶段 |
| T1 | 另一媒体复述同一公告 | 可能增加来源证据；命题等价不自动产生新的交易催化；链接到已送命题即为已知，不重复通知 |
| T2 | 原记录增加“首期产能为 10 万件” | 保存 Item 修订并抽取新增范围；形成可定位的新增数量 / 条件，独立判断是否通知 |
| T3 | 公司更正为“5 万件” | 更正明确指向旧 claim refs；保留原有历史，生成适用的 source_update |
| T4 | 公司宣布实际投产 | 这是动作阶段的新证据，不能仅凭日历到达就提前推断 |

T2 的卡片生成失败不应回滚 T2 的知识；T3 更正不会改写 T0 冻结的研究 Case，也不会自动平掉账户现有仓位。

<a id="section-验证与排障入口"></a>
## 08 · 验证与排障入口

先定位 `event_id`、来源修订、wanted / done、content revision、intent，再看[输入范围](../../tests/news/test_news_update_input_scope.py)、[语义 Worker](../../tests/news/test_news_semantic_worker.py)、[通知规则](../../tests/news/test_news_event_update_notifications.py)以及[修订存储](../../tests/integration/test_news_revision_ownership.py)、[EventUpdate 存储](../../tests/integration/test_news_event_update_store.py)、[发送集成](../../tests/integration/test_news_update_delivery.py)。

前端通过新闻流与 Event 详情读取这些维度；历史回执按其真实版本展示，不由 UI 合成当前命题。列表计数与卡片成功率、语义采用率的分母不同，不应混算。

代码测试证明状态、身份、引用与副作用边界；真实新闻理解质量仍需独立复核。保留的[ReviewDesk / 校准](review.md)不是自动优化发布系统。

[Issue 717 固定窗口与离线回放记录](../reports/issue-717-hourly-comparison-2026-09-27.md)保存 #718 对重复命题、实际发送与延时的历史比较。它不是本手册整理时重新执行的生产测试，也没有测得部署后的模型调用次数与延时改善。

<a id="section-源码责任地图"></a>
## 09 · 源码责任地图

| 所有者 | 主要职责 |
| --- | --- |
| [receiver.py](../../tracefold/news/pipeline/receiver.py)、[recovery.py](../../tracefold/news/pipeline/recovery.py) | 接收 OpenNews，记录中断与有界恢复，将原始输入交给 broker |
| [admission.py](../../tracefold/news/pipeline/admission.py) | 区分来源契约，保存 Item、确定性拆分和 Event 归组，提交证据与语义工作 |
| [events](../../tracefold/news/events/) | FactUnit 范围、grounding、准入、身份、标题 / token / MinHash 候选匹配 |
| [semantic.py](../../tracefold/news/pipeline/semantic.py) | 消费语义唤醒，领取版本工作、执行尝试、退避、熔断与失败结算 |
| [updates/service.py](../../tracefold/news/updates/service.py) | `NewsAgent` 编排、采用与可选补读；`Notifications` 独立续接 |
| [semantics.py](../../tracefold/news/updates/semantics.py)、[judgment.py](../../tracefold/news/updates/judgment.py) | 引文校验、命题比较、有限问题、内容组装 |
| [dspy_backend.py](../../tracefold/news/updates/dspy_backend.py) | DSPy 抽取、中文文案、生成式判断与原生有限选项判断 |
| [notification.py](../../tracefold/news/updates/notification.py)、[reader_judgments.py](../../tracefold/news/updates/reader_judgments.py) | 逐命题决定表、读者新颖度、锚点与增量重要性判断、稳定意图与冻结卡片 |
| [event_update_store.py](../../tracefold/news/storage/event_update_store.py)、[event_updates.py](../../tracefold/news/storage/event_updates.py)、[update_commit.py](../../tracefold/news/storage/update_commit.py) | 短事务、检查点、共用 EventUpdate 提交、head 条件采用、计划和发送账本 |
| [public.py](../../tracefold/news/updates/public.py) | 从已采用知识生成公开更新，不依赖读者卡片 |
| [delivery.py](../../tracefold/news/pipeline/delivery.py)、[maintenance.py](../../tracefold/news/pipeline/maintenance.py) | 通知轮询、真实投递、补唤醒与有界保留清理 |

<a id="section-常见误解"></a>
## 10 · 常见误解

<details>
<summary><strong>展开常见问题</strong></summary>

**有 EventUpdate，为什么没有卡片？**

知识采用不等待文案。继续看逐命题计划、intent 和实际发送账本；来源数量增加也不必然产生新的通知。

**新来源复述同一说法，是否必然再推送？**

不必然。来源可以只增加证据；重复抑制看实际已发正文的完整覆盖，而非 Event 名称或相似标题。

</details>

---

[返回文档中心](../README.md) · [架构图谱](../ARCHITECTURE.md#atlas) · [返回顶部](#news增量新闻理解与独立通知)
