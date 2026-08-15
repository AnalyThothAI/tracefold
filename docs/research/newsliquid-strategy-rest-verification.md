# 本地最新版 OpenNews MCP：Strategy REST / MCP / WSS 合同核查

核查日期：2026-08-13

核查对象：用户指定的本地仓库 `/Users/massis/Documents/Code/opennews-mcp`。本报告逐项读取 README、三份本地化文档、`knowledge/guide.md`、OpenClaw skill、`src/opennews_mcp/api_client.py`、全部 MCP tools、应用生命周期与 server 注册入口；未修改该仓库，也未读取或输出 token 值。

## 更正说明

先前报告的主要结论“公开 `opennews-mcp` 没有 Strategy 触发历史 REST”仍成立，但证据范围和 WSS/MCP 关系需要两项明确更正：

1. **证据范围更正**：先前以临时 clone 的固定 GitHub commit 为基线，没有明确审计用户提供的本地 checkout，也漏列了本地 [`knowledge/guide.md`](/Users/massis/Documents/Code/opennews-mcp/knowledge/guide.md:128) 与 [`openclaw-skill/opennews/SKILL.md`](/Users/massis/Documents/Code/opennews-mcp/openclaw-skill/opennews/SKILL.md:48)。本次确认本地 checkout 恰好与先前固定 commit 相同，且新增审计的知识文件也没有 Strategy 历史 REST 或专用 Strategy MCP tool。
2. **MCP 能力表述更正**：不能笼统说“MCP 完全收不到 Strategy”。正确表述是：**没有 Strategy 专用 MCP tool，也没有历史 REST tool；但通用 `subscribe_latest_news` 使用同一个 WSS，接收函数不按 JSON-RPC `method` 过滤。结合官方“Strategy owner 连接后自动收到、无需额外订阅”的合同，Max owner 的 `strategy.triggered` 帧有可能在该通用工具的短监听窗口内被原样收进结果。** 这是代码与文档共同支持的推断，不是一个专用、可恢复的 Strategy MCP 合同。[实时工具](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/tools/realtime.py:9) [原样接收](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/api_client.py:551) [自动 per-user push](/Users/massis/Documents/Code/opennews-mcp/README.md:475)

## 结论

| 问题 | 核查结果 |
|---|---|
| OpenNews 是否公开 Strategy 历史 REST？ | **否。** 本地 HEAD 的 REST client 没有 Strategy endpoint；README、知识指南和工具清单也没有。[REST client 全部公开端点](https://github.com/6551Team/opennews-mcp/blob/695fa3cd201a629aab0f79b754e0305ca99c999a/src/opennews_mcp/api_client.py#L130-L504) |
| 是否有 Strategy 专用 MCP tool？ | **否。** server 注册的 discovery、finance、free、news、realtime 五个模块中没有 Strategy tool。[注册入口](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/tools/__init__.py:1) [官方工具表](/Users/massis/Documents/Code/opennews-mcp/README.md:219) |
| 是否能通过 MCP/WSS 收到 live Strategy？ | **官方 WSS 可以；通用 MCP 实现可能原样带回。** Max owner 的连接自动收到 `strategy.triggered`；通用工具不识别或过滤 message method，但只短时监听、随后关连接。[官方 Strategy push](/Users/massis/Documents/Code/opennews-mcp/README.md:414) [MCP 监听与关闭](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/tools/realtime.py:55) |
| `/open/news_search` 的 `score: 70` 是触发历史吗？ | **否。** 它是普通新闻最低 AI 分数过滤；没有 Strategy ID。`get_high_score_news` 强制只查 `page=1`。[搜索请求](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/api_client.py:137) [高分工具](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/tools/news.py:225) |
| NewsLiquid 网页是否有事件搜索 REST 实现？ | **当前部署前端有** `POST /news-platform/v1/strategy-events/search`，但它不在本地 OpenNews MCP 合同中，故应定性为账户产品的未公开实现面，而非公开 OpenNews API。[当前部署 API client](https://app.newsliquid.com/_next/static/chunks/6006-f27e4d375f13f3aa.js) |
| 能否按 Strategy ID、时间或游标可靠补拉？ | **公开合同不能。** WSS payload 含 `strategy.id` 与 `ts`，但没有历史查询、时间窗口、分页、cursor 或 replay 请求语义。[Strategy 字段](/Users/massis/Documents/Code/opennews-mcp/README.md:459) |

## 1. 本地 Git 基线

只读命令 `git status --porcelain=v2 --branch`、`git log -1`、`git remote -v` 和 `git rev-list --left-right --count HEAD...origin/HEAD` 得到：

| 项目 | 值 |
|---|---|
| 本地路径 | `/Users/massis/Documents/Code/opennews-mcp` |
| branch | `main` |
| upstream | `origin/main` |
| HEAD | `695fa3cd201a629aab0f79b754e0305ca99c999a` |
| commit 时间 | `2026-08-05T18:29:48+08:00` |
| commit subject | `feat(finance): Added function for querying politicians' stock activities and institutional holdings` |
| 当前本地 refs 的 ahead/behind | `+0 / -0` |
| worktree | clean；无 staged、modified 或 untracked 文件 |
| origin fetch/push | `https://github.com/6551Team/opennews-mcp.git` |

固定 permalink：[`695fa3cd201a629aab0f79b754e0305ca99c999a`](https://github.com/6551Team/opennews-mcp/tree/695fa3cd201a629aab0f79b754e0305ca99c999a)。为遵守“不修改该仓库”，本次没有执行 `git fetch`；`+0/-0` 是相对本地已记录 `origin/HEAD` 的结果，不额外声称远端在核查瞬间没有更新。

## 2. 完整注册链：没有隐藏的 Strategy MCP tool

MCP server 启动时导入 `opennews_mcp.tools`，依靠 import 触发全部 `@mcp.tool()` 注册。[server.py](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/server.py:12) `tools/__init__.py` 只导入：

- `discovery`
- `finance`
- `free`
- `news`
- `realtime`

见本地 [`tools/__init__.py`](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/tools/__init__.py:6) 与 [固定 permalink](https://github.com/6551Team/opennews-mcp/blob/695fa3cd201a629aab0f79b754e0305ca99c999a/src/opennews_mcp/tools/__init__.py#L1-L10)。

对这些模块的所有 `@mcp.tool()` 静态枚举，与 README 和 knowledge 的工具表一致：新闻发现、普通搜索、高分/信号筛选、Finance Enhance、免费热点，以及一个 `subscribe_latest_news`；没有 `get_strategy_events`、`search_strategy_history`、`subscribe_strategy` 或同义工具。[README 工具表](/Users/massis/Documents/Code/opennews-mcp/README.md:219) [knowledge 工具表](/Users/massis/Documents/Code/opennews-mcp/knowledge/guide.md:128) [knowledge 实时项](/Users/massis/Documents/Code/opennews-mcp/knowledge/guide.md:162)

应用生命周期也只有三个 client：`FreeNewsAPIClient`、`NewsAPIClient`、`NewsWSClient`；配置 token 后才创建后两者，没有第四个账户/Strategy client。[app.py](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/app.py:10) [client 创建](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/app.py:25)

对本地 HEAD 执行：

```text
git grep -n -i 'strategy\|strategy-events\|strategy.triggered' HEAD -- \
  README.md docs knowledge openclaw-skill src/opennews_mcp
```

命中仅出现在英文 README 与日/韩/中文 README 的 WSS Strategy push 章节；`knowledge/`、OpenClaw skill 和 `src/opennews_mcp/` 均无 Strategy 字样。这是“本地 HEAD 没有 Strategy 专用实现”的完整仓库级核查，不是只检查单个 client 方法。

## 3. REST：普通新闻搜索，不是 Strategy 历史

### Endpoint 与鉴权

`NewsAPIClient` 为所有认证 REST 请求注入：

```text
Authorization: Bearer <OPENNEWS_TOKEN>
Content-Type: application/json
```

见 [`api_client.py`](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/api_client.py:77) 和 [固定 permalink](https://github.com/6551Team/opennews-mcp/blob/695fa3cd201a629aab0f79b754e0305ca99c999a/src/opennews_mcp/api_client.py#L77-L97)。`OPENNEWS_TOKEN` 从环境变量或 `config.json.api_token` 读取；环境变量优先。[config.py](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/config.py:22) README 指向 `https://6551.io/mcp` 获取 6551 API Bearer Token。[README](/Users/massis/Documents/Code/opennews-mcp/README.md:249)

普通新闻检索的唯一 endpoint 是：

```text
POST https://ai.6551.io/open/news_search
```

其 body 由 client 明确构造为：

| 参数 | 类型/默认 | 语义 |
|---|---|---|
| `limit` | integer，client 默认 20 | 每页数量 |
| `page` | integer，client 默认 1 | 1-based 页码 |
| `coins` | string[]，可选 | 币种过滤 |
| `q` | string，可选 | 全文关键词 |
| `engineTypes` | map，可选 | engine/source 过滤 |
| `hasCoin` | boolean，可选 | 仅带币种条目 |
| `score` | integer，可选 | 最低 AI score，0–100 |

代码来源：[请求构造](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/api_client.py:137)；参数公开说明还明确 `limit` 为 1–100、`page` 为 1-based、`score` 为最低 AI 分数。[OpenClaw HTTP 指南](/Users/massis/Documents/Code/opennews-mcp/openclaw-skill/opennews/SKILL.md:114)

该请求没有 `strategyId` / `strategyIds`，也没有起止时间、cursor、trigger ID 或 owner/account 参数。因此它能分页搜索普通新闻索引，但不能以公开合同证明“某个用户 Strategy 曾在某时触发”。

### `get_high_score_news` 的真实行为

MCP tool 参数只有：

- `min_score`，默认 70；
- `limit`，默认 10、经 `clamp_limit` 限制；
- 隐式使用配置中的 `OPENNEWS_TOKEN`。

实现固定调用 `api.search_news(score=min_score, limit=limit, page=1)`，然后在客户端排序并返回：

```json
{
  "success": true,
  "min_score": 70,
  "data": [],
  "count": 0,
  "total": 0
}
```

见 [`news.py`](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/tools/news.py:225) 和 [固定 permalink](https://github.com/6551Team/opennews-mcp/blob/695fa3cd201a629aab0f79b754e0305ca99c999a/src/opennews_mcp/tools/news.py#L225-L249)。它不暴露 `page`，不接受时间范围，也不携带 Strategy ID。故 `/open/news_search` 支持普通页码不等于 `get_high_score_news` 能遍历历史，更不等于 Strategy trigger history。

## 4. WSS：官方 Strategy 合同与通用 MCP 工具的关系

### 官方 WSS 合同

README 公布的 endpoint 是：

```text
wss://ai.6551.io/open/news_wss?token=YOUR_TOKEN
```

普通新闻订阅需要发送 `news.subscribe`，可选参数是 `engineTypes`、`coins`、`hasCoin`。[README WSS](/Users/massis/Documents/Code/opennews-mcp/README.md:283) [订阅参数](/Users/massis/Documents/Code/opennews-mcp/README.md:293)

Strategy 则是另一种 server notification：

```json
{
  "jsonrpc": "2.0",
  "method": "strategy.triggered",
  "params": {
    "id": 1234567890,
    "ts": "2025-01-15T08:30:00Z",
    "strategy": {
      "id": 42,
      "name": "BTC Funding Rate Alert",
      "sourceType": "market",
      "metrics": {}
    }
  }
}
```

事实合同是：

- 需要 Max subscription；
- event 由 NATS per-user 推给拥有该 Strategy 的用户；
- 无需发送 Strategy 订阅消息，连接后自动接收；
- payload 有 event `id`、`ts`、`strategy.id`、名称、source type、触发 metrics，以及可选 AI score。

来源：[英文 README](/Users/massis/Documents/Code/opennews-mcp/README.md:414) [字段](/Users/massis/Documents/Code/opennews-mcp/README.md:459) [per-user 自动推送](/Users/massis/Documents/Code/opennews-mcp/README.md:475)；中文镜像给出相同语义。[中文 README](/Users/massis/Documents/Code/opennews-mcp/docs/README_ZH.md:423) [中文自动推送说明](/Users/massis/Documents/Code/opennews-mcp/docs/README_ZH.md:484)

### 本地 WSS client

`NewsWSClient` 使用同一 `API_TOKEN` 拼在 WSS query string 中。[api_client.py](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/api_client.py:508) `subscribe_latest()` 总会先发送：

```json
{
  "method": "news.subscribe",
  "id": "req_...",
  "params": {
    "engineTypes": {},
    "coins": [],
    "hasCoin": false
  }
}
```

仅有真实非空过滤才会进入 `params`；随后它把第一帧当作 subscribe response。之后 `receive_news()` 只是 `json.loads()` 并返回任何 JSON 帧，不检查 `method == "news.update"`。[subscribe_latest](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/api_client.py:529) [receive_news](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/api_client.py:551)

### `subscribe_latest_news` MCP tool

工具参数与边界：

| 参数 | 默认/限制 | 作用 |
|---|---|---|
| `wait_seconds` | 默认 10，clamp 到 1–30 | 每次 receive 的 timeout；不是历史时间范围 |
| `max_items` | 默认 5，clamp 到 1–20 | 最多读取帧数；不是服务端分页 |
| `coins` | comma-separated，可选 | 转为 news.subscribe coin filters |
| `engine_types` | `type:cat,...;...`，可选 | 转为 news.subscribe engine filters |
| `has_coin` | 默认 false | news.subscribe filter |

它先调用 `news.subscribe`，再把 `receive_news()` 返回的原始 dict 逐个放进 `items`，响应为 `{"success":true,"data":items,"count":len(items)}`，最后无条件关闭 WSS。[realtime.py](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/tools/realtime.py:9) [读取与响应](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/tools/realtime.py:55) [关闭](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/tools/realtime.py:74)

由此得到三层结论：

1. **事实**：官方 owner-specific Strategy 帧无需额外订阅；本地 generic receiver 不按 method 过滤。
2. **合理推断**：若 WSS token 对应拥有 Strategy 的 Max 用户，而且事件恰在工具监听期间触发，该 raw `strategy.triggered` 帧可能出现在 `subscribe_latest_news.data` 中。
3. **不能推导**：这不是 Strategy 专用工具，不按 Strategy ID 过滤，没有 server-side history、时间范围、分页、cursor、ack、resume 或 replay；单次 receive timeout 最多 30 秒、最多读取 20 帧且调用结束即断开，也使它不适合持续可靠接入。

## 5. NewsLiquid 当前前端的私有事件搜索：与本地 OpenNews MCP 分层

2026-08-13 当前部署前端仍可观察到：

```text
POST https://ai.6551.io/news-platform/v1/strategy-events/search
Authorization: Bearer <NewsLiquid frontend auth-state token>
```

前端 Strategy 页面发送 `{"limit":50}`，选中 Strategy 时增加 `"strategyIds":[<number>]`；store 期待 `{data,total}`。证据来自当前部署的 [API client chunk](https://app.newsliquid.com/_next/static/chunks/6006-f27e4d375f13f3aa.js)、[Strategy page chunk](https://app.newsliquid.com/_next/static/chunks/app/strategy/page-31a7c1f884a4abdc.js) 与 [Strategy store chunk](https://app.newsliquid.com/_next/static/chunks/6876-8d92b3875d052ae6.js)。

但本地 `opennews-mcp` 的完整 REST endpoint 列表没有 `/news-platform/*`，所有认证 REST 路由均位于 `/open/*`。[本地 client](/Users/massis/Documents/Code/opennews-mcp/src/opennews_mcp/api_client.py:130) 当前前端 auth store 取 `userInfo?.token ?? guestAuthToken`，也不是本地 MCP 代码所命名的 `OPENNEWS_TOKEN` 配置路径。[部署 auth chunk](https://app.newsliquid.com/_next/static/chunks/3041-1db4a0c358f796c3.js)

此前只读运行时快照还显示：同一枚已配置 operator OpenNews token 调用公开 `/open/news_search` 返回 `200`，调用 `/news-platform/v1/strategy-events/search` 返回 `403`。这只证明该 token/账户在该时点无账户侧 endpoint 权限，不能证明所有 token 永远不同；但足以说明不能把私有前端 endpoint 与本地 OpenNews MCP Bearer 合同直接拼接。

该前端调用也未观察到 `page`、`offset`、cursor、起止时间或排序字段。后端可能支持未公开参数，但本地仓库、前端调用和公开文档都没有提供可依赖的合同。

## 6. 最终合同矩阵

| 能力 | endpoint / tool | credential | Strategy ID | 分页/时间 | 响应语义 | 稳定性判断 |
|---|---|---|---|---|---|---|
| 普通新闻搜索 | `POST /open/news_search` | `OPENNEWS_TOKEN` Bearer | 无 | raw REST 有 `limit/page`；无时间 | 普通新闻 JSON；MCP tools 再包装 | 本地公开合同 |
| 高分新闻 MCP | `get_high_score_news(min_score, limit)` | 同上 | 无 | 固定 `page=1`；无时间 | `success,min_score,data,count,total` | 本地公开合同，但非 Strategy |
| 普通 live MCP | `subscribe_latest_news(...)` | 同一 token 的 WSS query | 无 | 1–30 秒 receive、1–20 帧；无 replay | 原样 JSON frames 包在 `data` | 公开 generic tool |
| Strategy live | WSS `strategy.triggered` | WSS token + Max + owner 关系 | payload 有 `strategy.id`，无请求过滤 | 无历史/分页/cursor；payload 有 `ts` | JSON-RPC notification | 官方公开 Strategy 合同 |
| Strategy 历史 REST | 本地仓库中不存在 | 未定义 | 未定义 | 未定义 | 未定义 | **无公开合同** |
| NewsLiquid 网页事件搜索 | `POST /news-platform/v1/strategy-events/search` | 前端 account/guest auth-state Bearer；具体 entitlement 未公开 | UI 可发 `strategyIds[]` | 仅观察到 `limit` | UI 期待 `data,total` | 当前部署私有实现观察 |

## 7. 对 Tracefold 的可执行判断

- 若目标是官方支持的 Strategy 输入，只能把 `strategy.triggered` 当作 **owner/account-bound live WSS event**，不能当作公共、非个性化新闻流。
- 可以复用现有 WSS 连接/JSON decode 基础设施，但必须按 `method` 显式分流，并保留 `strategy.id` 与 owner/provider 配置边界；不能依赖通用 MCP tool 的偶然 raw-frame 行为。
- 目前没有公开的 Strategy REST recovery 合同。若要求断线补偿，需 NewsLiquid 正式确认 server credential、历史 endpoint、时间/cursor 分页、排序、保留期、去重 key、与 WSS 的一致性及 replay 保证。
- 在获得该合同前，应把 Strategy live 定义为 best-effort/no replay；不要用 `/open/news_search?score=70` 冒充已触发事件历史，也不要把网页私有 endpoint 当作 OpenNews MCP 稳定 API。
