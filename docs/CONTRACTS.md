# 公开契约与接口参考

[手册](README.md) · [架构](ARCHITECTURE.md) · [生成参考](generated/README.md) · [前端](FRONTEND.md)

本页维护**公开接口的意义、边界与当前入口**。精确字段、参数、枚举与约束由代码及生成物维护，不在这里复制另一份完整 schema。

<details>
<summary><strong>本页目录</strong></summary>

1. [事实来源](#section-事实来源)
2. [当前 HTTP 接口](#section-当前-http-接口)
3. [News：输入、知识与通知身份](#section-news输入知识与通知身份)
4. [市场、钱包与价格](#section-市场钱包与价格)
5. [Trading、操作与执行](#section-trading操作与执行)
6. [CLI 与配置](#section-cli-与配置)
7. [时间、缺失与版本规则](#section-时间缺失与版本规则)

</details>

<a id="section-事实来源"></a>
## 01 · 事实来源

| 契约 | 权威来源 | 生成 / 验证 |
| --- | --- | --- |
| HTTP | [routes](../tracefold/app/http/routes/)、[schemas](../tracefold/app/http/schemas/) | [OpenAPI](generated/openapi.json)、[漂移测试](../tests/contract/test_openapi_drift.py) |
| 前端 API 类型 | OpenAPI | [openapi.ts](../web/src/lib/types/openapi.ts)，`make regen-contract` |
| CLI | [parsers](../tracefold/app/cli/parsers/)、[commands](../tracefold/app/cli/commands/) | [生成帮助](generated/cli-help.md) |
| 配置 | [models.py](../tracefold/platform/config/models.py)、[loader.py](../tracefold/platform/config/loader.py) | 初始化生成配置与校验测试 |
| 数据库 | [Alembic versions](../tracefold/platform/postgres/alembic/versions/) | [db-schema.md](generated/db-schema.md)，隔离迁移数据库生成 |
| News 知识与通知 | [updates/contracts.py](../tracefold/news/updates/contracts.py)、[notifications/contracts.py](../tracefold/news/notifications/contracts.py) | 引用、版本、采用、实际正文与发送测试 |
| Trading 执行交接 | [executor/core.py](../tracefold/trading/executor/core.py)、[operator_control.py](../tracefold/trading/operator_control.py) | Signal v4、操作员意图与执行决策 |

生成物保留机器标识和源语法；文档中文化不改变 JSON 字段、CLI 参数、错误码或协议版本。

<a id="section-当前-http-接口"></a>
## 02 · 当前 HTTP 接口

公开 `/api/*` **全部只读**，通过 [router.py](../tracefold/app/http/router.py)挂载。Serve 数据库 pool 也保持只读，不为一个已删控制页面保留隐藏写能力。

| 方法与路由 | 回答的问题 |
| --- | --- |
| `GET /api/bootstrap` | 当前浏览器读取会话所需的 bootstrap 数据 |
| `GET /api/status` | 系统运行角色的已记录状态与测量时钟 |
| `GET /api/news/feed` | 新闻流、过滤、分页和相应计数 |
| `GET /api/news/events/{event_id}` | 一个 Event 的证据、知识、工作与通知详情 |
| `GET /api/news/items/{item_id}/events` | 按 Item 分页读取全部成员 Event，包括非 leader；返回范围、wanted/done、采用、决定、意图与送达摘要 |
| `GET /api/news/status` | News 能力、处理进展及相关诊断 |
| `GET /api/news/market` | 类型化市场观察组列表 |
| `GET /api/news/market/{item_id}` | 单个市场 Item 的原始数据与组内上下文 |
| `GET /api/news/quotes` | 类型化资产的当前报价快照及新鲜度 |
| `GET /api/news/symbols/{base}` | 标的身份及对应阅读接缝 |
| `GET /api/news/wallets` | 名单、采集进度与钱包系统状态 |
| `GET /api/news/wallets/events` | 净买入 episode 列表 |
| `GET /api/news/wallets/events/{episode_id}` | episode 首报 / 当前证据、成员与价格观察 |
| `GET /api/trading/status` | 交易分析与执行的已记录状态 |
| `GET /api/trading/cases` | Case 列表；`?source_item_id=<OI观察ID>` 定位关联 Case，`?case_id=<64位hex>` 返回冻结输入、预测、六策略与两腿纸面结果 |
| `GET /api/trading/scoreboard` | 指定窗口和可选 Program SHA 的漏斗、策略与预测质量 |
| `GET /api/trading/executions` | 从原生观察与计划派生的执行记录及覆盖 |

另有 `GET /healthz`、`GET /readyz`、`GET /metrics`。探针归属所在角色；不同进程的就绪语义不能互换。请求 envelope、错误响应、查询上下界和字段空值定义请直接查 OpenAPI。

`/api/news/status` 将可领取的 `semantic_pending`、等待调度的 `semantic_deferred`、租约中的 `semantic_in_progress` 与终结的 `semantic_failed_exhausted` 分开。`semantic_failed_exhausted` 计入当前仍失败的 revision，不论失败前实际用了几次尝试；失败 revision 不属于可运行 pending；计数不是推送次数或模型调用次数。

Event 详情的 `processing.notification.state` 为 `pending` / `done` / `failed`；`failed` 是通知工作的持久终态，带 `last_error_code`，对应结果 `outcome.kind = notification_failed`（归入“被拦截”），只有 `news retry-work --kind notification` 按精确 content revision 重开。逐命题原因 `send_outcome_ambiguous` 表示此前发送结果不明、按可能已送达处理且不重发；`send_outcome_unresolved` 只表示本 Event 仍有发送进行中。`processing.notification.plan.origin` 为 `reader_v2` 时，逐命题行另带 `novelty`（known / increment / development / in_flight / unlinked）、`render`（full / increment / correction）、`importance` 与分布、`reader_backend`；`editorial_v1` 历史只给出旧原因。

### 认证与浏览器行为

`/api/bootstrap` 返回名为 `ws_token` 的读取 bearer。字段名是历史拼写，不证明仍有 WebSocket。该令牌不提供命令权限，也不是独立保护整个公开站点的身份系统；对外服务时需保护整个工作台 origin，见[安全](SECURITY.md)。

页面通过有界 HTTP 查询 / 轮询读取，缓存和 ETag 不能替代 freshness。某字段为 null、记录不存在、源数据陈旧、部分结果与请求失败都必须按契约区分。

<details>
<summary><strong>历史参考：已移除的路由</strong></summary>

以下不是可调用兼容入口，应返回未找到而不是重定向到一个假功能：

<!-- retired-routes:begin -->

`/ws`、`/app`、`/app/*`、`/api/recent`、`/api/events/by-ids`、`/api/search`、`/api/search/inspect`、`/api/token-case`、`/api/target-posts`、`/api/target-social-timeline`、`/api/live-market`、`/api/token-images/*`、`/api/token-radar`、`/api/stocks-radar`。

`/api/news/wallets/cards`、`/api/trading/signals`、`/api/trading/execution/observations`、`/api/trading/execution/state`、`/api/trading/gate`、`/api/trading/gate/{event_id}`。

`POST /api/trading/execution/commands` 及该路径的 GET 都已移除；不保留公开下单 / 控制端点。

<!-- retired-routes:end -->

该标记块供现有契约测试区分当前路由与历史路径；删除接口时同步更新正文、生成物与调用方，不保留漂移的备用路径。

</details>

<a id="section-news输入知识与通知身份"></a>
## 03 · News：输入、知识与通知身份

| 身份 | 契约意义 |
| --- | --- |
| 提供商记录与 Item | 来源记录身份；相同输入重投不创建额外事实 |
| 来源修订序号 / 前驱 | 同一记录的版本出现顺序；正文哈希不能替代修订身份 |
| `input_revision` | 语义工作处理的证据版本，不等于采用次数 |
| `read_ref` | 某 Event 在来源修订下的任务阅读范围及投影契约；同来源不同范围分别结算 |
| `content_revision` | 已采用知识版本，不等于模型调用次数或推送次数 |
| `claim_ref` / evidence ref | 命题、引文与更正目标的稳定引用，不用裸标题代替 |
| notification intent | 针对读者与精确内容的稳定发送意图 |
| notification decision | 不可变的 `reader_v2` 决定：逐命题原因、读者新颖度、锚点与增量重要性分布、作答后端和冻结输入摘要；工作和意图引用其身份；`editorial_v1` 历史只读 |
| claim link | 采纳时从 `changes` 写入的命题比较（`news_claim_links`），只追加，按两端 ref 双向读取 |
| card copy input digest | 所选命题的完整表达材料和文案器身份；仅相同实际输入复用中文文案 |
| 冻结正文 / 实际发送账本 | 谁可能收到什么、结果是否已明确 |

新采用内容使用 `news_event_update_v2`；旧 v1 保留原始内容与哈希。Event 详情读取当前 adopted update、来源、语义工作、通知决策及真实发送回执。只有旧事实的 Event 仍可看到来源和实际回执，但不再生成旧 verdict 的详情投影，也不会由旧 verdict 合成新 Claim。

公开编辑型契约 `news_public_update_v1` 区分 `catalyst_delta` 与 `source_update`。前者给合格变化内容一个研究入口，后者显式更新旧 claim refs，可能跨 Event；它不创建新研究有效期或自动影响已有仓位。

[News 文档](modules/news.md)解释抽取与采用，[主题和来源](modules/news.md#topics-and-cited-source-authority)解释 IPTC 与引用来源身份，不将来源权威、事实已兑现和交易授权混成一个字段。

<a id="section-市场钱包与价格"></a>
## 04 · 市场、钱包与价格

| 契约 | 语义限制 |
| --- | --- |
| 类型化市场观察 | 保留 source contract、测量单位和解析状态；未知不补零 |
| OI | 变化百分比、名义金额和 measurement 定义不能替代实际仓位方向 |
| 钱包 episode | first / current / send snapshot、每地址资格、覆盖截止和实际观察时点分别保存 |
| Quote Snapshot | 当前展示值和 freshness，不是历史成交价 |
| `reaction_v2` | 类型化标的、固定 5m K 线、1h / 4h 新闻后价格反应 |

符号必须结合资产类别与交易所原生身份。基础币符号、股票 ticker、倍数合约和 USDT / USDC 市场不能靠字符串相等推断经济等价。

新闻报价使用 GET /api/news/quotes 的 assets 参数：JSON 数组，每项包含 symbol 与 market_type，例如 [{"symbol":"V","market_type":"equity"}]。请求、响应和客户端缓存按市场类型与符号区分，返回实际 venue/venue_symbol；不保留旧 symbol-only 参数。未知市场不选同名合约报价。Feed/Detail 有语义 head 时展示 current 有效命题的 primary，未采用时显示已有来源资产；来源 grounded_assets 仍供证据查看。仅参考目录、未知或缺行情都保留新闻与资产，不充当准入条件。

<a id="section-trading操作与执行"></a>
## 05 · Trading、操作与执行

`SignalV4` 与 `OperatorIntentV1` 的身份与作用域由执行契约定义，模型不能自由添加未知字段来绕过编译器。Signal 绑定 Case / decision、几何、映射、账户槽位、entry scope 与截止时间；订单与成交由 DEMO 场所对账确认。

CaseView v2 保留标的/原生单位/相对年龄/覆盖/episode；evaluator/run/assessment/action 分开，Signal.decision_id 引用实际 action。legacy参数仍unknown，assessment终态不可改。scoreboard.programs 保留字段名但按run_id区分，带evaluator/mode/manifest；原始/校准预测分开，PIT显示覆盖、配对失败/缺失不填零、七日/十簇不足区间为空。capture_cohort使用持久relay_capture_v1启动/outbox落库/受理边界分开backlog与prospective，来源年龄和上游ingest_mode另列，存量边界未知继续unknown；backlog不自动发布。status.active_faults为未解决故障，last_error为历史摘要；totals仅归因Plan成交/手续费，未覆盖资金费/转账/未归因历史。

本地 `trading issue` 使用关闭的命令语法，必须有稳定 `--request-id` 和调用方封存的 `--requested-at-ns`，重试保留两者。它记录本地 OS 认证的意图，不证明交易所动作完成。

执行结果只从与 Plan 精确绑定的交易所原生成交计算。原生成交按交易所交易 ID 去重，延迟归因另记不可变绑定；缺少完整成交或 USDT 手续费时净收益显示未知。执行器周期性核验仍有责任的订单与账户。

<a id="section-cli-与配置"></a>
## 06 · CLI 与配置

| CLI 组 | 当前职责 |
| --- | --- |
| `init` / `config` | 初始化唯一用户配置与脱敏查看 |
| `serve` / `workers` / `analysis` | 各自进程入口，不共享一套隐式生命周期 |
| `db` | migrate、health、audit、query-audit 与运行身份相关操作 |
| `news` | broker、目录、ReviewDesk、校准、离线 replay、钱包诊断、why 与精确 retry-work |
| `trading` | status / diagnose、Case / scoreboard / replay / Signal / fill 查询、本地操作与历史核验 |

`news replay` 在本地重放 provider hits 的准入 / Gate，不调用模型或 broker；它也不代表完整 EventUpdate→通知→交易回放。实际参数和默认值见[生成 CLI 帮助](generated/cli-help.md)，不要把命令名称扩展为未实现能力。

Trading replay 默认 policies/source-run，复用输出零LM；inference/program才隔离新推理并关cache，均不发布/访问交易所，同run幂等、tag区分新评估。calibrate声明训练/未来验证，仅写候选，不启用。llm.max_shared_concurrent_calls为同实际endpoint跨进程总slots，各owner配置须一致。

未知配置 key 按 Settings 拒绝。`llm.news_reader_judgment` 是通知决策层独用的 System One 路由，密钥只能以 `api_key_file` 引用配置目录下的私有文件（固定为初始化创建、只挂载给 Workers 的 `news_reader_judgment_api_key`；空文件等于未配置），`config` 与 `/api/news/status` 只报告是否配置、模型和作答后端。密钥放在配置允许的位置 / 文件，不能通过 `llm.request.extra_body` 注入 transport-owned 字段或秘密。业务配置只有 `TRACEFOLD_HOME/config.yaml`，默认 `~/.tracefold/config.yaml`；Compose 可读取 `.env` 持久化项目、路径和端口，但 Settings 不把它作为业务字段回退。

<a id="section-时间缺失与版本规则"></a>
## 07 · 时间、缺失与版本规则

来源发布时间、本地首次收到、语义完成、实际发送、市场目标 / 观察时间与交易所成交时间是不同的时钟。字段名包含 `_ms` 或 `_ns` 时按对应单位处理，不能把 model completion 当作新的 source first-available。

不可变记录的新语义需要新版本；缓存和 UI 投影可重建，不可以反过来改写旧事实。缺失收益、未知发送结果与未完成 provider 请求必须显式表示。端到端重放依靠稳定身份和可检查条件，不宣称所有外部副作用天然 exactly-once。

---

[返回文档中心](README.md) · [架构图谱](ARCHITECTURE.md#atlas) · [返回顶部](#公开契约与接口参考)
