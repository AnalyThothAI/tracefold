# Frontend：中文只读工作台

[手册](README.md) · [系统架构](ARCHITECTURE.md) · [接口契约](CONTRACTS.md) · [开发](DEVELOPMENT.md)

工作台负责把**已记录的事实、理解、决策与实际结果**呈现清楚，而不是把后端复杂度藏在一个绿色状态点后面。它不生成另一份业务账本，不在页面请求时重跑模型，也不提供浏览器下单入口。

| 模块速览 | 说明 |
| :--- | :--- |
| **定位** | 工作台 / 只读呈现 |
| **运行位置** | React、路由、Feature Query 与 Serve GET |
| **输入 → 产物** | URL 状态和已记录的 News / Trading 投影 → 可分享的列表、冻结 Case 详情、策略记分板与执行监控 |

> [!IMPORTANT]
> 页面不写订单、不重跑模型；Query 缓存、URL 筛选和短暂交互不重复保存同一事实。

[接口契约](CONTRACTS.md) · [前端测试范围](TESTING.md)

<details>
<summary><strong>本页目录</strong></summary>

1. [从 URL 到已记录证据](#section-从-url-到已记录证据)
2. [实际路由与页面职责](#section-实际路由与页面职责)
3. [新闻阅读与详情交互](#section-新闻阅读与详情交互)
4. [行情、钱包和执行的状态展示](#section-行情钱包和执行的状态展示)
5. [API 类型与生成物](#section-api-类型与生成物)
6. [视觉与交互约定](#section-视觉与交互约定)
7. [本地开发与验证](#section-本地开发与验证)
8. [源码责任地图](#section-源码责任地图)
9. [常见误解](#section-常见误解)

</details>

<a id="section-从-url-到已记录证据"></a>
## 01 · 从 URL 到已记录证据

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
    accTitle: 只读工作台的数据与状态
    accDescr: URL 由路由管理，服务器状态由 Query 管理，应用会话提供读取令牌，纯派生结果交给展示组件。FastAPI 查询持久记录。
    URL["URL<br/>可分享的筛选与详情"] --> Route["React Router<br/>路由与页面装配"]
    Session["应用会话<br/>bootstrap 读取令牌"] --> Client["类型化 HTTP 客户端"]
    Route --> Query["Feature API / Query<br/>服务器状态与刷新"]
    Query --> Client
    Client <-->|GET / 响应| API["FastAPI<br/>已记录证据的只读投影"]
    API -->|只读查询| DB[("PostgreSQL")]
    Query --> Model["Feature model<br/>纯派生与标签"]
    Model --> View["Feature UI + shared/ui<br/>加载、正常、空、陈旧、错误"]

    classDef news fill:#ecfdf5,stroke:#0f766e,color:#134e4a,stroke-width:1.5px
    classDef research fill:#eef2ff,stroke:#6366f1,color:#312e81,stroke-width:1.5px
    classDef execution fill:#fff7ed,stroke:#c2410c,color:#7c2d12,stroke-width:1.5px
    classDef store fill:#f1f5f9,stroke:#64748b,color:#1e293b,stroke-width:1.5px
    classDef external fill:#f8fafc,stroke:#94a3b8,color:#334155,stroke-dasharray:4 3
class URL,Route,Session,Client,Query,API,DB,Model,View store;
```

*前端视图 · URL、服务器缓存与短暂交互各有所有者；页面读取不会重跑模型或向账户下单。*

`/api/bootstrap` 返回当前读取所需的 `ws_token`。这个历史字段名不代表系统使用 WebSocket；当前页面通过 HTTP 查询 / 轮询读取，没有浏览器命令写权限。

Query 管理服务器状态，URL 管理可分享的筛选和详情位置，组件 state 管理短暂交互。不要再用全局 store 复制同一批记录，否则刷新、分享链接和浏览器后退容易看到三份不同状态。

[AppRoot](../web/src/app/AppRoot.tsx)提供一个 QueryClient，默认重试一次、8 秒 stale time、窗口聚焦时不自动刷新；各 feature 可覆盖这些设置。[useAppSession](../web/src/app/useAppSession.ts)获取 bootstrap 后配置读取 token，只有会话就绪才启用业务 Query。路由的 `ShellChromeRoute` 先构造外壳，`ShellRoute` 再处理 bootstrap 加载 / 失败并提供页面会话，News 与 Trading 路由按需加载。

[HTTP 客户端](../web/src/lib/api/client.ts)只使用同源 GET；开发时由 Vite 代理 `/api`。认证使用 Bearer，token 变化会清空 ETag 响应缓存。请求键必须包含实际过滤条件、游标和类型化资产身份；304 复用对应已验证响应，仍按响应内的业务时钟解释 freshness。

| 查询 | 当前刷新策略 | 所有者 |
| --- | --- | --- |
| 新闻流 / 标的 Event 列表 | 最新窗口每 3 秒；25 条 / 页，最多三页；旧窗口暂停轮询 | [newsQueries](../web/src/features/news/api/newsQueries.ts) |
| Event 详情、News 状态、当前报价 | 每 15 秒，报价与新闻内容分别缓存 | News API |
| 市场观察、钱包状态与 episode 列表 | 每 10 秒；历史页与原始 Item 详情单独查询 | News API |
| 外壳基础状态 | 每 15 秒；共享 News 状态 Query，不从 News 页面轮询 Trading | [cockpit API](../web/src/features/cockpit/api/useCockpitStatusQuery.ts)、[shellChromeData](../web/src/routes/shellChromeData.ts) |
| Trading Case、记分板与执行记录 | 每 15 秒，冻结输入与逐步完成的结果一起读取 | [tradingQueries](../web/src/features/trading/api/tradingQueries.ts) |
| Trading 账户状态 | 每 1 秒，窗口聚焦强制刷新，响应设置 `no-store` | Trading API |

Trading 账户状态将网络往返时间扣除后端给出的 `facts_remaining_ms`，再由 [useTradingFactExpiry](../web/src/features/trading/state/useTradingFactExpiry.ts)在有效期到达时失效展示。一次成功请求不延长账户事实的有效期。

<a id="section-实际路由与页面职责"></a>
## 02 · 实际路由与页面职责

以 [router.tsx](../web/src/routes/router.tsx)为准：

| 路径 | 页面问题 | 主要数据 |
| --- | --- | --- |
| `/` | 导向新闻首页 | 重定向 `/news` |
| `/news` | 最近采用或通知的新闻是什么 | 新闻流与过滤 |
| `/news/events/:eventId` | 一件新闻如何从证据走到知识与通知 | EventUpdate、语义进度、意图和实际回执 |
| `/news/status` | News 哪些能力正常，哪里没有推进 | 业务状态、时间与错误 |
| `/news/market` | 当前哪些类型化市场观察值得查看 | 观察组、通知节奏与具名原因 |
| `/news/market/:itemId` | 这一条市场记录到底解析了什么 | 原始 Item、测量、组内上下文 |
| `/news/wallets` | 哪些代币出现集中净买入 | 名单 / 覆盖、episode、成员、首报与当前快照 |
| `/news/symbols/:base` | 这个类型化资产关联哪些信息 | 身份、新闻、报价与窗口数据 |
| `/trading` | 研究做了什么，账户实际执行了什么 | Case / 六策略记分板、执行记录与角色状态 |

状态页“主要资产市场分布”读取 `/api/news/status` 的 `primary_asset_markets_24h`，展示最近 24 小时已采用解析的 primary 资产总数、市场未定数量及占比、各已知市场数量。比例由后端计算；空样本显示“暂无主要资产样本”，不写成 0%。该观测不另算健康状态，不改变新闻处理或推送。

Trading 的 tab / case 等页面选择由 URL 与路由会话保留。React 未匹配的路径显示未找到；Serve 只挂载 `/`、`/news`、`/news/{path:path}` 与 `/trading` 的 SPA 入口，其他宿主路径返回 404。News 子路径交给 React 匹配，不能用静态入口的 200 证明该页面存在。

<a id="section-新闻阅读与详情交互"></a>
## 03 · 新闻阅读与详情交互

Event 详情应分别呈现：最新 wanted 输入是否完成、当前 adopted head 是哪一版、命题与引文如何变化、为什么选择 / 不选择通知、哪个 intent 的正文真正发出。
“新增了什么”中的一条当前命题只展示一次；它与多条历史命题的比较并不代表多条新增报道或命题。
详情概览下方使用同一张主内容卡片，顶部四个 tab 为“事件内容”“来源证据”“当前行情”“处理记录”；同时只显示一个 panel，点击原位切换，不沿页面跳转。桌面右侧保留“通知与送达”上下文；手机按活动 tab、通知与送达、技术详情排列。历史比较、引用与结构化字段在所属命题内展开，同类报道放入来源 tab，时间线、发送记录与投递回执放入处理 tab；符号归一和关联 Item 查询集中在技术折叠。

`tab` query 取 `content`、`source`、`market` 或 `processing`，配合可选 `focus` 管理可分享的阅读位置，例如 `?tab=source&focus=source-01`；刷新和后退恢复对应 tab。左右方向键与 Home / End 切换 tab，选中项与 panel 有无障碍关联。命题来源、来源关系和发送入口通过回调切换所属 tab，打开必要记录并以 `preventScroll` 聚焦；目标超出真实工作台视口时带入视口。隐藏 panel 保留 DOM 与展开状态；内容槽保留已阅读高度，避免切换到短内容时浏览器调整滚动位置。复制事件链接保留 `tab` / `focus`；未知 tab 使用内容页，缺失 focus 不创建业务状态。

没有 adopted head 时仍保留四个 tab，内容使用原始标题与来源说明，来源保留真实报道。契约没有独立译文，命题展示已记录原文。详情刷新失败保留最后成功内容、缓存读取时间和真实重试；行情读取失败只影响行情 tab。只有 `sent` 意图的正文称为“实际发送正文”，其他状态显示冻结意图正文；发送结果不明时不能确认已送达，也不暗示自动重发。

| 不能混用 | 正确展示 |
| --- | --- |
| 新输入失败与已有知识无效 | 保留可读旧 head，同时说明最新语义失败 |
| 卡片生成成功与提供商发送成功 | 展示意图、冻结内容和独立真实回执 |
| 所选 claim refs 与正文完整覆盖 | 以实际发送正文为准，不用选择集合冒充覆盖证据 |
| 历史发送回执与新 Claim | 只显示实际发送事实，不由旧记录合成当前命题 |
| 新闻流计数与推送次数 | 各用对应查询定义，不把一个 Event 的多次通知重复计成多条 Event |

News 状态还要分开 `semantic_pending`、`semantic_deferred`、`semantic_in_progress` 与 `semantic_failed_exhausted`；耗尽工作不能画成仍可自动运行的 pending。

新闻流和资产页的 Event 列表由同一个有界分页 Query 管理，每次最多保留三页。新闻流中的事件行在桌面与手机均直接进入详情页，不再打开右侧预览抽屉；行标题保持原生链接，键盘 Enter、修饰键和中键沿用浏览器链接行为，资产链接仍进入标的页。当前搜索与筛选随路由传入详情页的返回入口；同一读取会话保留列表滚动位置和分页代次，返回链接与浏览器后退在列表内容就绪后恢复位置。进入另一件事件从详情顶部开始阅读，详情内的 tab 切换保持原位；独立打开的详情链接不虚构列表顺序。市场研究的 Item 侧栏维持原交互。第一页轮询与后续页共用同一过滤身份；翻到较旧窗口后暂停轮询，并提供“返回最新”重新从第一页读取。滚动锚点只保存 Event ID，新版本内容始终从当前查询结果读取。列表的页首统计与第一页事件由同一数据库语句返回；详情中的历史回执保留原始版本，当前标题和状态按当前 head 与通知工作解释。

正文旁资产来自同一有效语义 head 的 primary；无 head 时展示来源关联资产，不把原始 grounded_assets 再追加到采用后的资产列表。行情请求、Query key 和返回索引使用 market_type + symbol，避免同 ticker 的股票与加密资产共用价格。未知市场或仅参考目录的资产保留可读标签，不划为错误 token，也不借另一市场报价；目录状态由后端给出。来源原始标签仍可在证据中查看。超出既有报价输入范围的长地址或名称保留展示，单独退出行情批次，不影响其他 ticker；不满足目录路径范围的资产显示原词，不生成不可访问的标的链接。

<a id="section-行情钱包和执行的状态展示"></a>
## 04 · 行情、钱包和执行的状态展示

**未知不等于零，过期不等于缺失，部分完成不等于失败。** 行情应显示来源、价格种类和时钟；钱包应区分 first、current、send snapshot；执行页应区分研究 action、发布、受理、成交、保护与平仓。

Event 与标的页面展示当前报价，保留价格种类、venue、来源 / 接收时钟和 fresh / stale / unavailable。24H 变化只作滚动市场参考，不是事件收益；钱包首报、当前与发送快照中的净流量属于钱包事实，不能由当前价格倒推历史参考价或收益。

请求失败时可以保留最后成功缓存，但必须标记数据陈旧与错误，不突然把整页变成“没有数据”。同一查询的加载、空数据、失败、部分数据和正常状态保持可理解的布局与重试入口。

全局健康探针不是所有业务能力的汇总完成证明。尤其不能把“Executor 进程在线”直接画成“账户已核实、已保护、费用齐全”。

<a id="section-api-类型与生成物"></a>
## 05 · API 类型与生成物

[openapi.json](generated/openapi.json)来自实际挂载的 FastAPI routes / schemas；[openapi.ts](../web/src/lib/types/openapi.ts)由它生成。手写 envelope 与类型别名留在 [frontend-contracts.ts](../web/src/lib/types/frontend-contracts.ts)，不要手改生成文件以绕过后端契约。

```bash
make regen-contract
```

仅当接口契约真的改变时重新生成并提交 JSON 与 TypeScript 的对应变化。纯文字文档不要求安装前端依赖或刷新无关 schema。

<a id="section-视觉与交互约定"></a>
## 06 · 视觉与交互约定

工作台优先呈现信息层级，而不是装饰性面板。使用现有 `PageShell`、`PageHeader`、`PageReadingContent` 及统一 token；保持内容宽度、间距、表格密度和详情区节奏一致，避免每个 feature 自建一套外观。

中文文案说明状态与下一步，英文保留精确契约名和代码标识。错误和风险不仅靠颜色表达；交互元素可键盘聚焦，有可辨识名称。长 ID、原文和模型解释提供适当换行 / 展开，不挤坏主要事实。窄屏主要按钮、来源链接与记录展开行使用 44px `--tap-target`，保持工作台真实滚动容器内的焦点可见。

顶部搜索服务于 News 阅读场景，不虚构一个覆盖全部产品的全局搜索。可分享过滤条件放入 URL，刷新和后退应恢复同一阅读位置。

<a id="section-本地开发与验证"></a>
## 07 · 本地开发与验证

后端地址与代理以 [vite.config.ts](../web/vite.config.ts)为准。不要把开发页面误接到未经授权的账户操作环境。

```bash
cd web
npm ci
npm run dev
```

在实际改动对应层面选择检查：

```bash
npm run typecheck
npm run lint
npm run test:unit
npm run build:checked
```

`lint` 已包含架构测试，单独诊断时可运行 `npm run test:architecture`。组件级渲染不证明整条页面路由可用；Mock API 的 Playwright 场景也不证明真实 FastAPI / 静态文件 / bootstrap 已贯通。

`npm run test:e2e` 与 `npm run test:e2e:full-stack` 的资源和证据范围不同，见 [TESTING](TESTING.md)。改变视觉与路由后实际查看页面，检查桌面 / 窄屏、加载 / 错误 / 空数据、导航和控制台错误；不要仅凭 build 成功宣称体验已验收。

<a id="section-源码责任地图"></a>
## 08 · 源码责任地图

当前前端由 React 19、React Router 6、TanStack Query、TypeScript、Vite 和 Tailwind 构成；版本范围见 [package.json](../web/package.json)，实际解析版本由锁文件决定。

| 目录 | 所有者职责 |
| --- | --- |
| [src/app](../web/src/app/) | 应用装配、全局 provider 与会话接缝 |
| [src/routes](../web/src/routes/) | 路由定义、URL / 路由会话、页面装配和错误边界 |
| [src/features](../web/src/features/) | News、Trading、cockpit 等业务 feature 的公开入口 |
| feature 内 `api` | 查询键、请求、缓存和刷新；不让组件重复组织同一请求 |
| feature 内 `model` | 纯派生、格式与状态解释；不请求网络 |
| feature 内 `state` / `ui` | 交互状态 / 展示，不重复存储 URL 与服务器事实 |
| [src/shared](../web/src/shared/) | 可复用布局、控件与展示接缝；不拥有 feature 数据读取 |
| [src/lib](../web/src/lib/) | HTTP 客户端、类型、基础工具 |
| [src/styles](../web/src/styles/) | 设计 token 与共享样式 |
| [tests](../web/tests/) | 单元、组件、路由、架构与浏览器行为验证 |

依赖从路由装配进入 feature 的公开入口；跨 feature 不深挖私有实现。公共组件不偷偷发查询，纯模型不藏网络副作用。

<a id="section-常见误解"></a>
## 09 · 常见误解

<details>
<summary><strong>展开常见问题</strong></summary>

**打开历史 Case 会重新分析吗？**

不会。页面读取冻结记录，不悄悄补入当前行情或另一次模型回答。

**ws_token 代表实时 Socket 或账户权限吗？**

不是。当前工作台通过 HTTP GET 读取；历史字段命名不改变只读边界。

</details>

---

[返回文档中心](README.md) · [架构图谱](ARCHITECTURE.md#atlas) · [返回顶部](#frontend中文只读工作台)
