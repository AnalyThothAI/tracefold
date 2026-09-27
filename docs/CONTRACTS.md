# 公开契约与接口参考

[手册](README.md) · [架构](ARCHITECTURE.md) · [生成参考](generated/README.md) · [前端](FRONTEND.md)

本页维护**公开接口的意义、边界与当前入口**。精确字段、参数、枚举与约束由代码及生成物维护，不在这里复制另一份完整 schema。

## 1. 事实来源

| 契约 | 权威来源 | 生成 / 验证 |
| --- | --- | --- |
| HTTP | [routes](../tracefold/app/http/routes/)、[schemas](../tracefold/app/http/schemas/) | [OpenAPI](generated/openapi.json)、[漂移测试](../tests/contract/test_openapi_drift.py) |
| 前端 API 类型 | OpenAPI | [openapi.ts](../web/src/lib/types/openapi.ts)，`make regen-contract` |
| CLI | [parsers](../tracefold/app/cli/parsers/)、[commands](../tracefold/app/cli/commands/) | [生成帮助](generated/cli-help.md) |
| 配置 | [models.py](../tracefold/platform/config/models.py)、[loader.py](../tracefold/platform/config/loader.py) | 初始化生成配置与校验测试 |
| 数据库 | [Alembic versions](../tracefold/platform/postgres/alembic/versions/) | [db-schema.md](generated/db-schema.md)，隔离迁移数据库生成 |
| News 知识与通知 | [updates/contracts.py](../tracefold/news/updates/contracts.py)、[notification.py](../tracefold/news/updates/notification.py) | 引用、版本、采用、实际正文与发送测试 |
| Trading 执行交接 | [execution_contracts.py](../tracefold/trading/execution_contracts.py) | Signal / 意图 / 观察与作用域测试 |

生成物保留机器标识和源语法；文档中文化不改变 JSON 字段、CLI 参数、错误码或协议版本。

## 2. 当前 HTTP 接口

公开 `/api/*` **全部只读**，通过 [router.py](../tracefold/app/http/router.py)挂载。Serve 数据库 pool 也保持只读，不为一个已删控制页面保留隐藏写能力。

| 方法与路由 | 回答的问题 |
| --- | --- |
| `GET /api/bootstrap` | 当前浏览器读取会话所需的 bootstrap 数据 |
| `GET /api/status` | 系统运行角色的已记录状态与测量时钟 |
| `GET /api/news/feed` | 新闻流、过滤、分页和相应计数 |
| `GET /api/news/events/{event_id}` | 一个 Event 的证据、知识、工作与通知详情 |
| `GET /api/news/status` | News 能力、处理进展及相关诊断 |
| `GET /api/news/market` | 类型化市场观察组列表 |
| `GET /api/news/market/{item_id}` | 单个市场 Item 的原始数据与组内上下文 |
| `GET /api/news/quotes` | 类型化资产的当前报价快照及新鲜度 |
| `GET /api/news/symbols/{base}` | 标的身份及对应阅读接缝 |
| `GET /api/news/wallets` | 名单、采集进度与钱包系统状态 |
| `GET /api/news/wallets/events` | 净买入 episode 列表 |
| `GET /api/news/wallets/events/{episode_id}` | episode 首报 / 当前证据、成员与价格观察 |
| `GET /api/trading/status` | 交易分析与执行的已记录状态 |
| `GET /api/trading/cases` | Case 列表与研究 / 发布结果 |
| `GET /api/trading/cases/{case_id}/replay` | 读取已冻结的研究材料，不重跑模型 |
| `GET /api/trading/executions` | 从原生观察与计划派生的执行记录及覆盖 |

另有 `GET /healthz`、`GET /readyz`、`GET /metrics`。探针归属所在角色；不同进程的就绪语义不能互换。请求 envelope、错误响应、查询上下界和字段空值定义请直接查 OpenAPI。

`/api/news/status` 将可领取的 `semantic_pending`、等待调度的 `semantic_deferred`、租约中的 `semantic_in_progress` 与终结的 `semantic_failed_exhausted` 分开。已耗尽 revision 不属于可运行 pending；计数不是推送次数或模型调用次数。

### 认证与浏览器行为

`/api/bootstrap` 返回名为 `ws_token` 的读取 bearer。字段名是历史拼写，不证明仍有 WebSocket。该令牌不提供命令权限，也不是独立保护整个公开站点的身份系统；对外服务时需保护整个工作台 origin，见[安全](SECURITY.md)。

页面通过有界 HTTP 查询 / 轮询读取，缓存和 ETag 不能替代 freshness。某字段为 null、记录不存在、源数据陈旧、部分结果与请求失败都必须按契约区分。

### 已移除的路由

以下不是可调用兼容入口，应返回未找到而不是重定向到一个假功能：

<!-- retired-routes:begin -->

`/ws`、`/app`、`/app/*`、`/api/recent`、`/api/events/by-ids`、`/api/search`、`/api/search/inspect`、`/api/token-case`、`/api/target-posts`、`/api/target-social-timeline`、`/api/live-market`、`/api/token-images/*`、`/api/token-radar`、`/api/stocks-radar`。

`/api/news/wallets/cards`、`/api/trading/signals`、`/api/trading/execution/observations`、`/api/trading/execution/state`、`/api/trading/gate`、`/api/trading/gate/{event_id}`。

`POST /api/trading/execution/commands` 及该路径的 GET 都已移除；不保留公开下单 / 控制端点。

<!-- retired-routes:end -->

该标记块供现有契约测试区分当前路由与历史路径；删除接口时同步更新正文、生成物与调用方，不保留漂移的备用路径。

## 3. News：输入、知识与通知身份

| 身份 | 契约意义 |
| --- | --- |
| 提供商记录与 Item | 来源记录身份；相同输入重投不创建额外事实 |
| 来源修订序号 / 前驱 | 同一记录的版本出现顺序；正文哈希不能替代修订身份 |
| `input_revision` | 语义工作处理的证据版本，不等于采用次数 |
| `content_revision` | 已采用知识版本，不等于模型调用次数或推送次数 |
| `claim_ref` / evidence ref | 命题、引文与更正目标的稳定引用，不用裸标题代替 |
| notification intent | 针对读者与精确内容的稳定发送意图 |
| 冻结正文 / 实际发送账本 | 谁可能收到什么、结果是否已明确 |

新采用内容使用 `news_event_update_v2`；旧 v1 保留原始内容。Event 详情可保留 `legacy_verdict` 作为历史读取，但 UI、公开 outbox 与新 Agent 不由旧 verdict 合成新 Claim。

公开编辑型契约 `news_public_update_v1` 区分 `catalyst_delta` 与 `source_update`。前者给合格变化内容一个研究入口，后者显式更新旧 claim refs，可能跨 Event；它不创建新研究有效期或自动影响已有仓位。

[News 文档](modules/news.md)解释抽取与采用，[主题和来源](modules/news.md#topics-and-cited-source-authority)解释 IPTC 与引用来源身份，不将来源权威、事实已兑现和交易授权混成一个字段。

### 保留的历史复核词表

`FACT_KINDS` 的代码顺序仍供旧 verdict / 接受复核读取：

`state_change|new_quantity|level_crossed|period_record|quantified_flow|official_measure|statement|recap|schedule|promotion`

它不是新 Claim 的完整语义模型。新流程将 `mode`、`phase`、`content_kind` 与命题关系分别表达；不能把旧 `statement` 一律删掉来判断当前通知。

## 4. 市场、钱包与价格

| 契约 | 语义限制 |
| --- | --- |
| 类型化市场观察 | 保留 source contract、测量单位和解析状态；未知不补零 |
| OI | 变化百分比、名义金额和 measurement 定义不能替代实际仓位方向 |
| 钱包 episode | first / current / send snapshot、每地址资格、覆盖截止和实际观察时点分别保存 |
| Quote Snapshot | 当前展示值和 freshness，不是历史成交价 |
| `reaction_v2` | 类型化标的、固定 5m K 线、1h / 4h 新闻后价格反应 |

符号必须结合资产类别与交易所原生身份。基础币符号、股票 ticker、倍数合约和 USDT / USDC 市场不能靠字符串相等推断经济等价。

## 5. Trading、操作与执行

`TradeSignalV3`、`OperatorIntentV1` 和 Runtime observation 的身份与作用域由执行契约定义，模型不能自由添加未知字段来绕过编译器。Signal 绑定 Case / decision、计划、映射、账户槽位、entry scope 与截止时间。

本地 `trading issue` 使用关闭的命令语法，必须有稳定 `--request-id` 和调用方封存的 `--requested-at-ns`，重试保留两者。它记录本地 OS 认证的意图，不证明交易所动作完成。

`trading verify-execution` 要求精确 entry、account slot、environment；默认只读预览，`--apply` 才追加核实的原生证据。浏览器与 News 推送通道不拥有此权限。

## 6. CLI 与配置

| CLI 组 | 当前职责 |
| --- | --- |
| `init` / `config` | 初始化唯一用户配置与脱敏查看 |
| `serve` / `workers` / `analysis` | 各自进程入口，不共享一套隐式生命周期 |
| `db` | migrate、health、audit、query-audit 与运行身份相关操作 |
| `news` | broker、目录、ReviewDesk、校准、离线 replay、钱包诊断、why 与精确 retry-work |
| `trading` | status / diagnose、Case / Signal / 观察查询、本地操作与历史核验 |

`news replay` 在本地重放 provider hits 的准入 / Gate，不调用模型或 broker；它也不代表完整 EventUpdate→通知→交易回放。实际参数和默认值见[生成 CLI 帮助](generated/cli-help.md)，不要把命令名称扩展为未实现能力。

未知配置 key 按 Settings 拒绝。密钥放在配置允许的位置 / 文件，不能通过 `llm.request.extra_body` 注入 transport-owned 字段或秘密。业务配置只有 `TRACEFOLD_HOME/config.yaml`，默认 `~/.tracefold/config.yaml`；Compose 可读取 `.env` 持久化项目、路径和端口，但 Settings 不把它作为业务字段回退。

## 7. 时间、缺失与版本规则

来源发布时间、本地首次收到、语义完成、实际发送、市场目标 / 观察时间与交易所成交时间是不同的时钟。字段名包含 `_ms` 或 `_ns` 时按对应单位处理，不能把 model completion 当作新的 source first-available。

不可变记录的新语义需要新版本；缓存和 UI 投影可重建，不可以反过来改写旧事实。缺失收益、未知发送结果与未完成 provider 请求必须显式表示。端到端重放依靠稳定身份和可检查条件，不宣称所有外部副作用天然 exactly-once。
