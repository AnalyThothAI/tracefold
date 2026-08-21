# Deep Agents × OpenTrade：主 Agent 真实下单权限的最小架构（方案 A）

> 状态：架构研究，不代表已经接入实盘；研究日期 2026-08-21。
>
> 结论基线：`deepagents==0.7.8`，官方 tag 对应不可变 commit `1e261ba201bb1af4dbc5cbc8b6424e709b850ea8`；OpenTrade 固定 commit `0efa9b4d27fc644a667453c5c41e55ad0d04557d`。
>
> 来源纪律：只采用 LangChain / LangGraph / Deep Agents 与 OpenTrade 的官方文档、官方仓库源码、测试、examples 和 release。网页文档会变化；一旦与锁定源码冲突，以锁定源码和测试为准。

## 结论先行

用户的目标是可实现的，而且不需要把交易执行移出 Agent：

1. **主 DeepAgent 应直接持有真实的 `place_order`、`cancel_order`、`close_position` 三个 venue-write tools。** 工具内部的 OpenTrade Adapter 使用真实凭证发出 `POST` / `DELETE`；因此是 Agent 发起并拥有真实能力，不是“Agent 只写 proposal、另一个 orchestrator 决定是否下单”。凭证对模型不可见，不等于模型没有调用权限。
2. **KISS 的正确位置是窄 Interface，不是弱权限。** 模型只提交 immutable `proposal_id + proposal_digest`；订单精度、CCXT symbol、hedged mode、数量、仓位、账户、密钥、OpenTrade payload、风险预占和异常对账全部由一个深的 `TradingOrderModule` 隐藏。
3. **提供三档静态 execution profile。** `paper` 绑定本地模拟 Adapter；`live_reviewed` 绑定真实 OpenTrade Adapter 并逐单 `approve/reject`；`live_bounded` 同样绑定真实 Adapter，但不逐单中断，主 Agent 可立即真实下单、撤单和平仓。profile 在 graph/thread 启动前由服务端固定，模型不能在 tool args 或 runtime state 中选择、切换或升级。
4. **Deep Agents harness 不是资金安全边界。** 官方 README 的安全模型就是“信任 LLM，并在 tool / sandbox 层限制能力”；所以 prompt、HITL、调用次数限制都只能是防御层，真正 invariant 必须在订单模块和数据库事务中实施。[官方安全说明](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/README.md#L110-L112)
5. **ToolNode 会并行执行同一模型消息中的多个 tool calls。** 锁定的 `langgraph-prebuilt==1.1.0` 同步路径使用 `executor.map`，异步路径使用 `asyncio.gather`；因此不能靠“模型通常一次只下一个单”。账户/合约级数据库锁、原子风险 reservation 和 proposal 唯一约束才是正确性边界。[ToolNode source](https://github.com/langchain-ai/langgraph/blob/3614e88c58af63f597764218646e85c49952b2da/libs/prebuilt/langgraph/prebuilt/tool_node.py#L819-L858)
6. **交易写调用绝不进入 `ToolRetryMiddleware`。** OpenTrade 固定文档未给 CEX order API 声明 client idempotency key；请求可能已经送达后的 timeout 必须记为 `AMBIGUOUS`，只允许查询和 reconcile，不允许盲重发。

这份方案修正此前“不给 DeepAgent live-order tool”的方向。确定性模块仍拥有风险、幂等和账本，但它现在是 **Agent tool 的 Implementation**，不是 Agent 外部的下单决策者。

## 1. 研究边界与版本冻结

### 1.1 锁定依赖快照

Deep Agents 的 release tag、package metadata 和官方 lockfile 给出以下可复现实证：

| Package | 锁定版本 | 官方证据 |
|---|---:|---|
| `deepagents` | `0.7.8` | [release](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.8)、[pyproject](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/pyproject.toml#L1-L30) |
| `langchain` | `1.3.15` | [official uv.lock](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/uv.lock#L939-L951) |
| `langchain-core` | `1.6.0` | [official uv.lock](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/uv.lock#L982-L1000) |
| `langgraph` | `1.2.11` | [official uv.lock](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/uv.lock#L1083-L1099) |
| `langgraph-prebuilt` | `1.1.0` | [official uv.lock](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/uv.lock#L1113-L1124)、[tag commit](https://github.com/langchain-ai/langgraph/tree/3614e88c58af63f597764218646e85c49952b2da) |
| `langgraph-checkpoint` | `4.1.1` | [official uv.lock](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/uv.lock#L1100-L1112) |

`deepagents` 自身的依赖合同是范围而非完整锁定（例如 `langchain>=1.3.15,<2`），且 package classifier 仍是 Beta。实现时应复制上述已验证组合进项目 lock，而不是只写 `deepagents>=...`。生产所需的 PostgreSQL checkpointer 是可选依赖，也必须另行固定版本并运行 crash/resume contract tests。

OpenTrade 研究固定在 commit [`0efa9b4`](https://github.com/6551Team/opentrade/tree/0efa9b4d27fc644a667453c5c41e55ad0d04557d)；它的 CEX skill metadata 是 `1.0.4`。[CEX skill header](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L1-L22)

### 1.2 与 Tracefold 当前边界的关系

Tracefold 当前架构明确只有 News V3，一个 PostgreSQL business truth，且没有 order/position execution lane；Deep Agents 也不是当前依赖。[当前 Architecture](../ARCHITECTURE.md)

所以本方案是一次**显式架构扩展**：新增独立 `TradingOrderModule` 和 trading deployable，不把订单状态塞进 `tracefold.news` 的 Event/Verdict/Delivery read models，也不让订单写入改变 News Gate/Triage/Policy。若接受该方向，GitHub Issue 必须同时修改“exactly one business capability”的现行决定；本研究文件本身不宣称该决定已经落地。

## 2. Deep Agents 官方实证

### 2.1 `create_deep_agent` 的真实 Interface

`0.7.8` 的完整关键参数为：

```text
model, tools, system_prompt, middleware, subagents, skills, memory,
permissions, backend, interrupt_on, response_format, state_schema,
context_schema, checkpointer, store, debug, name, cache
```

源码定义见 [`graph.py` L268-L288](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L268-L288)。最后它把 model、tools、middleware、response format、context/state schema、checkpointer 和 store 交给 LangChain `create_agent()`，返回配置了 recursion limit 的 compiled LangGraph。[assembly](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L911-L943)

这里最容易踩的坑是：

- `tools=` **只做 additive merge**，不能移除 filesystem / task 等 built-ins。移除 built-in 必须使用 HarnessProfile 的 `excluded_tools`，或用同名 middleware 替换默认 middleware。[source docstring](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L291-L339)
- 默认 backend 是 `StateBackend()`。[source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L619-L627)
- `model=None` 的隐式默认已 deprecated，应显式构造 model。[source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L140-L179)

方案 A 因此显式传 model、backend、checkpointer、context schema、subagents、tools 和 middleware，并在启动测试里断言最终可见工具集，而不是依靠 prompt 说“不要使用”。

### 2.2 built-in filesystem 并非默认安全

Deep Agents 会加入 `ls/read_file/write_file/edit_file/delete/glob/grep`，使用 sandbox 或 LocalShell backend 时还可能有 `execute`。[backend docs](https://docs.langchain.com/oss/python/deepagents/backends) `0.7.0` 又新增了递归 destructive `delete`，并改变了 `write_file` 为覆盖已有文件。[0.7 changelog](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/CHANGELOG.md#L59-L82)

官方提供的正确窄化手段是以同名 `FilesystemMiddleware` 替换默认实例，并传 `tools=[...]` allowlist；未列出的 built-ins 对模型不可见且不可执行。[FilesystemMiddleware source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/middleware/filesystem.py#L1620-L1673) 自定义 middleware 与默认项同名时会原位替换。[customization docs](https://docs.langchain.com/oss/python/deepagents/customization#middleware)

**锁定源码校验：** `0.7.8` 的实际 `FsToolName` 集合是 `ls/read_file/write_file/edit_file/delete/glob/grep/execute`；传 list 时 `read_file` 必须存在。方案 A 的精确只读 allowlist 是 `read_file/ls/glob/grep`，因此 `write_file/edit_file/delete/execute` 连 schema 都不暴露。

本方案只保留：

```python
FilesystemMiddleware(
    backend=StateBackend(),
    tools=["read_file", "ls", "glob", "grep"],
)
```

这会显式隐藏 `write_file`、`edit_file`、`delete` 和 `execute`。不用 `FilesystemBackend` 或 `LocalShellBackend`；研究 scratch 只在 graph state 中。真正证据和订单账本由业务数据库持久化，Agent filesystem 不是事实源。

`permissions=` 也不能充当交易权限系统。官方合同说明它只覆盖 built-in filesystem tools，不覆盖 custom/MCP tools，也不覆盖 sandbox 的任意命令执行；规则还是 first-match-wins、无匹配默认 allow。[permissions docs](https://docs.langchain.com/oss/python/deepagents/permissions) 因而 write-tool capability 必须靠工具注册范围、subagent 显式 tool set，以及 `TradingOrderModule` 的 server-owned mandate共同约束。

### 2.3 subagent tool scoping：默认行为会泄漏下单权限

这是本设计最重要的 Deep Agents 事实：

- Declarative `SubAgent` 未写 `tools` 时，会继承主 Agent 的 custom tools。[type/source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/middleware/subagents.py#L36-L125)、[assembly](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L726-L738)
- 没有显式覆盖时，Deep Agents 自动添加 `general-purpose` subagent；`0.7.8` 源码把主 Agent 的 `_tools` 原样给它。[source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L745-L814)
- 默认 GP 描述还明确说它拥有与主 Agent 相同的全部 tools。[source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/middleware/subagents.py#L285-L305)
- 官方 integration test 证明默认 GP 会继承父级 tools 和 `interrupt_on` 并执行它们。[test](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/tests/integration_tests/test_hitl.py#L109-L148)

因此只写“subagent 不得下单”的 system prompt 是错误的。可行机制有两种：

1. 通过 HarnessProfile 关闭 default GP；或
2. 显式声明一个同名 `general-purpose` spec 来覆盖 default，并给出完整只读 `tools`。

最终集成采用第 1 种：以 `GeneralPurposeSubagentProfile(enabled=False)` 关闭 default GP，再只注册两个有明确职责的 custom subagent；这比保留一个“万能但只读”的 GP 更符合 KISS。对**每一个** custom subagent 仍显式写 `tools=...`，任何 subagent 均看不到 `place_order/cancel_order/close_position`。官方也建议 clear description、详细 system prompt、最小 tool set 和 concise/typed result。[subagent best practices](https://docs.langchain.com/oss/python/deepagents/subagents#best-practices)

Subagent 的 `response_format` 可返回 Pydantic/dataclass/TypedDict/JSON Schema；结构化结果会 JSON serialize 为返回给 parent 的 ToolMessage。[source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/middleware/subagents.py#L127-L163) 本方案让 fact verifier 和 market analyst 返回 typed findings，而不是把大段自由文本和原始网页灌回主上下文。

V1 只使用 declarative synchronous subagents：它们在隔离上下文完成一个有界任务，主 Agent 等待 typed result 后再形成 thesis。官方 async subagents 是 preview，运行在各自 thread/Agent Protocol server，适合长任务和 mid-flight steering，[async docs](https://docs.langchain.com/oss/python/deepagents/async-subagents) 但会增加远端 lifecycle、身份和一致性 seam；KISS V1 不需要。无论未来是否启用，三个 venue-write tools 都不进入任何 sync/async subagent。

### 2.4 middleware 顺序、错误、重试与调用上限

`create_deep_agent` 的 base stack 是 skills（可选）→ filesystem → subagent → summarization → PatchToolCalls → async subagent（可选），之后插入 caller middleware，再接 profile/caching/memory/HITL 等 tail。[source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L360-L401) `0.7.8` 的实际 assembly 见 [`graph.py` L816-L893](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L816-L893)。

LangChain 官方 middleware 给出的有用机制及正确边界：

| Middleware | 官方语义 | 方案 A 用法 |
|---|---|---|
| `ToolErrorMiddleware` | 把 exception 转为受控 ToolMessage；官方建议只泄露 exception type，不把可能含 secret 的原始 message 给模型。[docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in#tool-error)、[1.3.15 source](https://github.com/langchain-ai/langchain/blob/f4bc5031dbcf24edb0374a07830915a285222567/libs/langchain_v1/langchain/agents/middleware/tool_error.py) | 只包装研究/行情 read tools；返回类型化、脱敏错误。交易 write boundary 自己返回 `ExecutionReceipt`，绝不把 OpenTrade raw body/token/header 给模型。 |
| `ToolRetryMiddleware` | 指定 tools、exception 和指数 backoff 自动重试；若与 ToolError 组合，retry 要放在更早的 inner 位置且 `on_failure="error"`。[docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in#tool-retry)、[1.3.15 source](https://github.com/langchain-ai/langchain/blob/f4bc5031dbcf24edb0374a07830915a285222567/libs/langchain_v1/langchain/agents/middleware/tool_retry.py) | **只列 RESEARCH_READ_TOOL_NAMES**，最多 2 次 transient retry。三个交易 write tools、proposal materialization 均不在列表。 |
| `ModelCallLimitMiddleware` | run/thread 维度限制 model calls，防止无限循环和成本失控。[docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in#model-call-limit)、[1.3.15 source](https://github.com/langchain-ai/langchain/blob/f4bc5031dbcf24edb0374a07830915a285222567/libs/langchain_v1/langchain/agents/middleware/model_call_limit.py) | 例如每次 20 次；到限即 error/no-new-entry。 |
| `ToolCallLimitMiddleware` | 可全局或按单个 tool 限制 run/thread calls；需要 checkpointer 才能保持 thread limit。[docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in#tool-call-limit)、[1.3.15 source](https://github.com/langchain-ai/langchain/blob/f4bc5031dbcf24edb0374a07830915a285222567/libs/langchain_v1/langchain/agents/middleware/tool_call_limit.py) | read tools 设置预算；三个 writes 各自 `run_limit=1`。另加一个小的 project middleware，拒绝同一 AIMessage 出现超过一个 trading write call。 |

调用上限是 harness guard，不是资金 invariant。即使 middleware 有 bug、模型并行调用、两个 Agent process 同时执行，数据库原子 reservation 仍必须阻止越额或双单。

另外，官方 customization 明确警告：并行 subagents、tools 和 invocations 会共享 middleware instance；不要在 middleware object 的 mutable attributes 中保存计数或授权状态。[custom middleware docs](https://docs.langchain.com/oss/python/deepagents/customization#middleware) 计数写 graph state/checkpointer，资金 reservation 写订单数据库。

### 2.5 HITL 与三档静态 profile

`interrupt_on` 会安装 LangChain `HumanInTheLoopMiddleware`，在敏感 tool **执行前**暂停。官方 `InterruptOnConfig` 支持 `when(ToolCallRequest) -> bool`：`False` 自动通过，`True` 才中断。[HITL docs](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop#conditional-interrupts) `ToolCallRequest.runtime.context` 可供 predicate 读取 server-owned runtime context。[LangChain 1.3.15 source](https://github.com/langchain-ai/langchain/blob/f4bc5031dbcf24edb0374a07830915a285222567/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L146-L210)

资金工具不应直接使用 `create_deep_agent(interrupt_on=...)` 这个便利参数：`0.7.8` 会把它装到 caller middleware 之后，而 LangChain 的 `after_model` hooks 逆序执行，可能先 interrupt、后运行自定义批量写检查。`live_reviewed` 应显式创建 `HumanInTheLoopMiddleware`，把它放在 user middleware 列表第一项；这样后列的 call limits / `SingleTradeWritePerModelMessage` 先验证，再进入 HITL。`paper/live_bounded` 则不安装该 middleware。[assembly source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py#L816-L934)、[middleware ordering](https://docs.langchain.com/oss/python/langchain/middleware/custom#middleware-order)

虽然官方支持 `when`，方案 A **不**用一个 runtime predicate 在 live/paper 间动态切换资金权限。三档 profile 在 Agent factory / deployment routing 时静态绑定 tool Implementation 和 HITL；同一个 thread 不能换档，变更 profile 必须新建已认证 execution/thread：

| 静态 profile | Write tool Adapter | 逐单 HITL | 主 Agent 的能力 |
|---|---|---:|---|
| `paper` | 本地 `PaperExecutionAdapter`，不持有 live secret、不访问真实 write endpoint | 否 | 可自主调用同名 place/cancel/close，但只改变模拟账本。OpenTrade 固定 CEX 文档没有 paper/testnet 合同，因此不伪称这是 OpenTrade paper。 |
| `live_reviewed` | 真实 `OpenTradeExecutionAdapter` + real credential capability | 是，只允许 `approve/reject` | 主 Agent 发起真实 write call；批准后原 tool Implementation 发到真实 endpoint。 |
| `live_bounded` | 真实 `OpenTradeExecutionAdapter` + real credential capability | 否 | 主 Agent 无需逐单人审，直接真实 place/cancel/close；仍受完整 mandate/risk/reservation/kill switch 约束。 |

profile 来自 operator-owned deployment/mandate 配置，不来自模型 args、用户 prompt、`TradingRunContext` 可变字段或 checkpoint。订单 Module 每次执行仍从 DB 重读 profile/mandate binding、有效期、kill switch 和 limits；graph 绑定与 DB 不一致时 fail closed。

三个 write tools 的 `allowed_decisions` 只包含 `approve/reject`，不开放 `edit` 或 `respond`。官方说明 `respond` 会产生 synthetic success，不适合拒绝 side-effect tool；大幅 edit 可能使模型重新规划并产生额外调用。[decision docs](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop#decision-types) 若 reviewer 要改数量/价格，必须 reject，主 Agent 重新 `prepare_trade_action` 生成新 digest。

HITL 必须配置 checkpointer；resume 必须使用同一个 `thread_id`，且 decisions 与 action requests 顺序一致。[HITL best practices](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop#best-practices) 官方 integration test 展示 interrupt 后用 `Command(resume=...)` 执行原 tools。[test](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/tests/integration_tests/test_hitl.py#L64-L106)

### 2.6 checkpointer、store 与 backend 是三件事

| 概念 | 官方责任 | 方案 A |
|---|---|---|
| `checkpointer` | thread-scoped graph snapshots；用于 conversation continuity、HITL、fault tolerance、time travel。[persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence) | 生产使用持久 PostgreSQL saver；测试才用 `InMemorySaver`。stable `thread_id`，设置 checkpoint retention。 |
| `store` | 跨 thread 的 application-defined long-term memory。[persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence) | V1 传 `None`。不让 Agent 的跨 thread 自由记忆影响资金决定。未来若启用，必须 tenant/user namespace，且不能保存 secret/authorization。 |
| `backend` | Deep Agents filesystem abstraction；默认 `StateBackend` 的文件在 graph state 中，thread-scoped。[backend docs](https://docs.langchain.com/oss/python/deepagents/backends#statebackend) | `StateBackend()` + read-only filesystem allowlist，仅作 context/scratch。 |
| `TradingOrderStore` | 不属于 Deep Agents，是业务模块的 durable truth。 | 保存 proposal、risk reservation、intent、attempt、remote receipt、reconciliation 和 audit。不能以 checkpoint/stream/model message 替代。 |

`MemorySaver/InMemorySaver` 重启即丢；官方生产建议 persistent saver，并提醒 checkpoints 会无限增长，需要 retention。[persistence troubleshooting](https://docs.langchain.com/oss/python/langgraph/persistence#troubleshooting-common-issues)

凭证不进入任何 prompt、runtime context、backend file、store、checkpoint、tool output 或 streaming event。`TradingRunContext` 只有 opaque tenant/principal/mandate/case/execution IDs；Adapter 在进程内根据这些 IDs 从 secret manager 获取 capability。

### 2.7 streaming 与 resume 不是订单事实

Deep Agents 当前 event streaming 用 `agent.stream_events(..., version="v3")`，可分别观察 coordinator、subagent、tool-call 的 started/completed/error 和 output deltas。[event streaming docs](https://docs.langchain.com/oss/python/deepagents/event-streaming) 它适合 operator console 的研究进度和可观测性。

但以下推断一律禁止：

- tool stream `completed` ≠ venue order filled；
- model 说“下单成功” ≠ venue accepted；
- stream 断开 ≠ tool 未执行；
- graph checkpoint 存在 ≠ remote side effect exactly once。

订单真相只读 `ExecutionReceipt + TradingOrderStore + OpenTrade/venue reconcile`。HITL 交互使用 `invoke/ainvoke(..., version="v2")` 取得 interrupts，并以同一 thread config 的 `Command(resume=...)` 继续；stream 只投影，不驱动资金 state machine。

LangGraph 官方还明确：resume 从 checkpoint boundary 重放，而非从 Python 同一行继续；已开始但未完成的 side effect 可能再次运行，因此写 API 必须有 idempotency key 或先查已存在结果。[Functional API determinism/idempotency](https://docs.langchain.com/oss/python/langgraph/functional-api#idempotency) 这正是本方案本地 intent/proposal 唯一约束和 `AMBIGUOUS` 语义的原因。

### 2.8 官方 deep-research example 能借鉴什么，不能照抄什么

锁定 commit 的官方 `deep_research` example：

- 主 Agent 和 researcher 只有 `tavily_search` / `think_tool`；custom subagent 显式写 tools。[agent.py](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/examples/deep_research/agent.py#L39-L59)
- prompt 默认偏向一个 subagent，只在明确可独立的比较维度并行，最多 3 个并行研究单元、3 轮 delegation。[prompts.py](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/examples/deep_research/research_agent/prompts.py#L138-L173)

方案 A 继承“窄角色、显式 tools、独立问题才并行、typed concise return”，但不把 example 的数字当安全控制：这两个上限只是 interpolated prompt 文本，不是 code enforcement。

还要注意官方 example drift：其 prompt 要求 `write_todos` 和 `write_file`，[example prompt](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/examples/deep_research/research_agent/prompts.py#L3-L18)；但 `0.7.0` 起 Deep Agents 默认已经移除 `TodoListMiddleware/write_todos`。[changelog](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/CHANGELOG.md#L63-L70) 生产代码必须 pin、测试最终工具集，不能从 example 猜默认值。

## 3. OpenTrade 官方能力与 Adapter 边界

### 3.1 真实写 endpoint

固定 CEX skill 明确提供：

- `POST /orders`：exact CCXT `symbol`、`side`、显式 `type`、`quantity` 或 `quoteAmount`，limit/trigger 的条件字段，以及必填 `hedged`。[Place Order](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L1264-L1388)
- `DELETE /orders/:orderId`：还需 exchange、symbol，TP/SL 订单可能需 type。[Cancel Order](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L1392-L1418)
- `POST /positions/close`：明确的 `quantity > 0` 与 `hedged`，不能以 0 代表全平。[Close Position](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L1628-L1669)
- open/closed orders、positions、trade/position history 等读 endpoint 可用于 reconcile。[Operation Flow](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L2069-L2097)

这意味着 Adapter 可真实执行 place/cancel/close；它不是 mock 或审批队列。主 Agent 调用 tool，tool 直接调用这些 endpoint。

### 3.2 不能外包给 OpenTrade 的风险

OpenTrade 文档声称 write endpoints 经过 price deviation、position size、rate limit 和 balance check；同时明确说明 market data 或 Redis 不可用时 risk engine **fail open**。[Risk Engine Notes](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L2132-L2152)

所以本地 `TradingOrderModule` 必须 fail closed。OpenTrade 风控只是 defense in depth，不能授权超出本地 mandate 的单。

固定的 Place Order 参数表列出了公开输入，但没有声明 CEX client order ID / idempotency key。[documented parameters](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L1324-L1338) 这是对该固定公开合同的**缺失观察**，不是断言上游内部永远不支持。因此 V1 的安全合同是：one network attempt + ambiguous reconcile；若上游以后正式提供 client idempotency，Adapter 可在不改变 Agent Interface 的情况下使用。

OpenTrade 文档使用 bearer token，并建议凭证不出现在日志、截图或聊天中。[pre-flight](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L29-L46) 生产实现不让 Agent 寻找 `.env`；专用 HTTP Adapter 从 secret manager 取 token 并注入 Authorization header。

## 4. 方案 A：一个深 Module，三个真实 write tools

### 4.1 Module 与 Seam

Module：`TradingOrderModule`

外部 Seam：主 DeepAgent 的五个交易相关 tools，其中只有三个触碰 venue。

```text
main DeepAgent
  ├─ prepare_trade_action(...)  local proposal / risk preflight
  ├─ inspect_trade(...)         read/reconcile
  ├─ place_order(...)           REAL POST /orders
  ├─ cancel_order(...)          REAL DELETE /orders/:id
  └─ close_position(...)        REAL POST /positions/close
                                  │
                                  ▼
                         TradingOrderModule
                  proposal + atomic risk reservation
                   intent journal + state machine
                         OpenTradeAdapter
                       real secret + HTTP
```

这条调用链中没有“Agent 外的 deterministic orchestrator 再决定是否下单”。`TradingOrderModule` 是三个 tools 的 Implementation：它把复杂度和硬约束局部化，但调用权在主 DeepAgent。

### 4.2 为什么是三个 write tools，而不是四十个 OpenTrade endpoints

恰好三个 venue-write tools：

1. `place_order(ref: ProposalRef) -> ExecutionReceipt`
2. `cancel_order(ref: ProposalRef) -> ExecutionReceipt`
3. `close_position(ref: ProposalRef) -> ExecutionReceipt`

分别映射三种不同风险语义，便于 tool scoping、HITL 描述、kill switch、rate limits、日志和测试。`set_leverage`、exchange credential config、wallet/signing、generic HTTP、shell/curl 不给模型。

`prepare_trade_action` 会写本地 immutable proposal，但不产生市场 side effect；`inspect_trade` 只读 journal/venue。它们不计入三个 venue-write tools。

### 4.3 Types

示意类型如下；金额和价格通过 decimal string 传输，禁止 binary float 成为业务真相：

```python
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

ExecutionMode = Literal["paper", "live_reviewed", "live_bounded"]
ActionKind = Literal["place", "cancel", "close"]

@dataclass(frozen=True)
class TradingRunContext:
    tenant_id: str
    principal_id: str
    mandate_id: UUID
    case_id: UUID
    agent_execution_id: UUID  # Across HITL resume, this remains stable.
    execution_mode: ExecutionMode  # Server-stamped audit value; must match static graph.

@dataclass(frozen=True)
class PlaceIntent:
    kind: Literal["place"]
    case_revision: int
    instrument_ref: str       # An internal stable ref, not a guessed symbol.
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit", "stop_market", "take_profit_market"]
    requested_notional: str
    limit_price: str | None
    trigger_price: str | None
    max_slippage_bps: int
    thesis_digest: str

@dataclass(frozen=True)
class CancelIntent:
    kind: Literal["cancel"]
    execution_id: UUID        # Must belong to this mandate/account/case.
    reason_code: Literal["thesis_invalid", "stale", "risk_reduce", "operator"]

@dataclass(frozen=True)
class CloseIntent:
    kind: Literal["close"]
    position_ref: str         # Server-owned exact venue position identity.
    close_fraction_bps: int   # 1..10_000; module computes actual quantity.
    max_slippage_bps: int
    reason_code: Literal["stop", "take_profit", "thesis_invalid", "risk_reduce"]

TradeActionIntent = PlaceIntent | CancelIntent | CloseIntent

@dataclass(frozen=True)
class ProposalRef:
    proposal_id: UUID
    proposal_digest: str

@dataclass(frozen=True)
class ActionProposal:
    ref: ProposalRef
    kind: ActionKind
    state: Literal["ready", "rejected"]
    expires_at_ms: int
    display_summary: str
    # Server-owned canonical fields include account, venue, exact CCXT symbol,
    # quantity, hedged/reduce semantics, snapshot hashes and policy version.

@dataclass(frozen=True)
class ExecutionReceipt:
    intent_id: UUID
    proposal_id: UUID
    kind: ActionKind
    state: Literal[
        "accepted", "rejected", "cancelled", "not_found", "stale", "ambiguous"
    ]
    remote_order_id: str | None
    observed_at_ms: int
    reconciliation_required: bool
    safe_next_actions: tuple[Literal["inspect", "new_proposal", "stop"], ...]
```

`ProposalRef` 是 write tool 的全部模型可编辑输入。交易所、账户、symbol、quantity、hedged、reduce-only、价格和风险结果都从 immutable proposal 重新读取；模型不能在 commit 阶段偷换。

`prepare_trade_action(intent, runtime)` 负责：

1. 验证 case/revision 和 evidence/thesis digest；
2. 将 issuer/ticker 解析成一个可交易的 exact venue instrument；无法唯一解析则 fail closed；
3. 读取 OpenTrade metadata、ticker、account、position mode、positions/open orders；
4. 根据 server-owned mandate 算 quantity、precision、exposure 和 freshness；请求超过上限时 reject，不静默 clamp；
5. 生成 canonical proposal、digest、expiry 和可审计 risk decision。

对 MRVL、GOOGL、SK Hynix、MRNA 这类新闻，只有当 OpenTrade metadata 真正返回 mandate 允许的精确可交易 instrument 时才生成 ready proposal；“新闻提到股票”不能推导出 OpenTrade 一定支持该证券。若只有 crypto venue 上的 TradFi derivative，也必须明确它不是现金股票。

### 4.4 Agent 的真实 capability 定义

下列条件同时成立时，称“DeepAgent 拥有真实下单权限”：

- `place_order/cancel_order/close_position` 出现在**主 Agent** tool registry；
- 主 Agent 的模型可自行发出这些 tool calls；
- tool Implementation 根据 runtime mandate 取得真实 credential capability；
- tool 直接调用 OpenTrade 的真实 write endpoint；
- 当前静态 graph 是 `live_bounded`，因此无逐单人审或另一个决策 orchestrator；
- venue result 直接写入业务 execution journal 并回给该 Agent。

方案 A 全部满足。把 token 藏在 Adapter 只是 secret containment；Agent 仍能导致 Adapter 使用 token，因此权限是真实的。

## 5. Hard invariants

以下 invariant 必须由 schema、数据库约束、事务和 Adapter 代码实施，不能只写入 prompt：

1. **Main-only write capability**：三个 venue-write tools 只在主 Agent；default `general-purpose` 被关闭，每个显式 subagent 只有明确 read tools。
2. **No generic escape hatch**：主 Agent 和 subagents 都看不到 `write_file/edit_file/delete/execute`、generic HTTP、shell、credential config、set leverage 或 arbitrary MCP call。
3. **Server-owned authority**：tenant、principal、account、mandate、execution mode、limits、kill switch 均来自 authenticated runtime/DB，模型 args 不能覆盖。
4. **Immutable commit**：write tool 只接受 `proposal_id + digest`；proposal 的 order/account/instrument/risk fields 一旦 ready 不可修改。
5. **Bound evidence**：place proposal 绑定 case revision、thesis/evidence digest、instrument mapping、market/account/position snapshot hashes、policy version和 expiry。
6. **Fresh recheck**：网络写前重读 mandate、kill switch、proposal state、reservation、账户/仓位/市场 freshness；任一变化使 proposal `stale`，不自动用新值提交。
7. **Fail closed locally**：价格、position mode、account、positions、metadata 或 risk state 缺失/冲突/过期时无 submit；OpenTrade fail-open 不得放宽。
8. **Atomic aggregate risk**：risk 计算包含 positions + open orders + 所有 `RESERVED/SENDING/AMBIGUOUS` 本地 intents，并在同一数据库事务内原子预占。
9. **Concurrency safe**：按 `(tenant, account, venue, instrument/risk_bucket)` 加锁；并行 ToolNode、多个 process、多个 thread 也不能越过 gross/net/notional/order-count limits。
10. **Stable tool intent identity**：用 server-owned stable execution scope 与 `runtime.tool_call_id` 派生本地 `intent_id`；模型不能提供。
11. **Proposal-level dedupe**：数据库另设 `UNIQUE(tenant_id, proposal_id, action_kind)`；同 proposal 即使产生不同 tool call IDs，也至多一次网络发送。
12. **No automatic write retry**：三个 writes 不在 `ToolRetryMiddleware`、broker retry 或 generic job retry 中。
13. **Ambiguity is first-class**：一旦请求可能离开进程但没有可信 terminal response，state=`AMBIGUOUS`；保留 risk reservation，阻止同 proposal/同 risk bucket 的新冲突写，只允许 inspect/reconcile。
14. **Receipt over prose**：模型消息、ToolMessage 文本和 stream state 均不能 terminalize order；只有 journal + typed Adapter result + venue reconciliation 能改变 execution state。
15. **Secret non-observability**：credential/header/raw upstream error 不进入 prompt、checkpoint、backend/store、stream、trace 或 tool result。
16. **No self-modifying policy**：Agent 不能编辑 mandate、risk policy、skills、system prompt、tool schemas、adapter config 或 secrets。
17. **Profile does not weaken risk**：`live_bounded` 相对 `live_reviewed` 只移除逐单人审，不移除任何 deterministic limit、reservation、freshness、dedupe、reconcile 或 kill switch；`paper` 则必须物理绑定无 live secret 的模拟 Adapter。
18. **Reduce actions remain explicit**：close/cancel 不是“免费无风险”；仍要 ownership、position/order identity、quantity、hedged mode 和 concurrency checks。账户冻结时可配置“禁止新开、允许 reduce-only”，但由 mandate policy 决定。

## 6. Ordering 与 execution state machine

### 6.1 一次 news-driven trade 的顺序

```text
News Event / 72 h case input
    │
    ├─ main Agent → fact-verifier subagent      (read-only, typed)
    ├─ main Agent → market-structure subagent   (read-only, typed)
    │                  only parallel if independent
    ▼
main Agent synthesizes bounded thesis
    ▼
prepare_trade_action(intent)
    ├─ resolve exact instrument
    ├─ fresh market/account/position reads
    ├─ deterministic risk/sizing
    └─ persist immutable proposal + digest + expiry
    ▼
main Agent calls the matching write tool
    ├─ paper         → same tool schema, simulated Adapter only
    ├─ live_reviewed → interrupt → approve/reject → same thread resume
    └─ live_bounded  → no interrupt, real Adapter
    ▼
DB lock → recheck → atomic risk reservation + attempt journal
    ▼
exactly one OpenTrade network attempt
    ├─ explicit accepted/rejected → persist receipt
    └─ maybe sent / unknown       → AMBIGUOUS
    ▼
inspect_trade / reconcile orders + trades + positions
    ▼
main Agent may later prepare + call cancel_order or close_position
```

Fact verification 先于 thesis；instrument resolve 先于 risk；risk 先于 commit；fresh recheck 紧邻 network write。主 Agent 可以根据研究主动选择不交易；但只要它调用 write tool，确定性 Module 就必须以同一顺序执行。

### 6.2 本地 states

建议最小 state machine：

```text
PROPOSAL_READY
      │
      ▼
RESERVED ──► SENDING ──► ACCEPTED ──► PARTIAL/FILLED/CANCELLED/CLOSED
   │             │             │
   │             └────────────► AMBIGUOUS ──► RECONCILED terminal/nonterminal
   │
   └────────────► REJECTED / STALE / EXPIRED
```

`SENDING` 必须在 HTTP 前持久化。`AMBIGUOUS` 不是 error string，而是占用风险预算的 durable state。只有 reconcile 证明“未创建订单”后，才可释放 reservation 并允许一个新的 proposal；不能把原 proposal 自动重发。

### 6.3 `intent_id` 与并行正确性

`ToolRuntime` 在锁定 `langgraph-prebuilt==1.1.0` 中包含 `tool_call_id`、context、config 和 store。[source](https://github.com/langchain-ai/langgraph/blob/3614e88c58af63f597764218646e85c49952b2da/libs/prebuilt/langgraph/prebuilt/tool_node.py#L837-L850)

推荐：

```text
intent_id = UUIDv5(
    TRADING_INTENT_NAMESPACE,
    tenant_id | thread_id | agent_execution_id | tool_name | tool_call_id,
)
```

其中 `agent_execution_id` 是业务服务生成、跨 HITL resume 保持不变的稳定 ID；不要使用每次 resume 可能变化的 callback/LangSmith run ID。再以 `UNIQUE(tenant_id, proposal_id, action_kind)` 防止模型对同 proposal 生成两个 tool calls。

write tool transaction 的最小算法：

1. 解析 server context，派生 `intent_id`；
2. 读取新鲜 remote snapshots；
3. 开数据库事务并获取 account/risk-bucket lock；
4. 若 proposal/action unique 已存在，返回既有 receipt/state；
5. 重读 mandate、kill switch、proposal/digest/expiry 和 aggregate exposure；
6. 原子插入 risk reservation + intent/attempt (`RESERVED`)，提交；
7. 将 attempt 标记 `SENDING` 后做**一次** OpenTrade call；
8. 按明确响应写 accepted/rejected；任意 post-send transport uncertainty 写 `AMBIGUOUS`；
9. 释放或保留 reservation，依据 terminal/reconciliation state，而非异常类型猜测。

DB reservation 是并行正确性的必要条件。`parallel_tool_calls=False`（模型/provider 支持时）、`SingleTradeWritePerModelMessage` 和 ToolCallLimit 都可减少意外并行，但都只是 defense in depth。

## 7. Error modes

| Mode | 可观察结果 | 自动重试 | Module 行为 / Agent 可做什么 |
|---|---|---:|---|
| schema / decimal / enum invalid | `proposal_rejected: invalid_intent` | 否 | 未触网；Agent 可修正 intent 后重新 prepare。 |
| issuer/instrument ambiguous 或 unsupported | `proposal_rejected: instrument_unresolved` | 否 | 不猜 symbol；补证据或不交易。 |
| case revision / thesis digest 变化 | `stale` | 否 | 原 proposal 失效；重新研究和 prepare。 |
| price/account/position/mode/metadata stale | `stale` 或 `risk_unavailable` | read 可有限重试；write 否 | fail closed；新鲜数据到达后生成新 proposal。 |
| mandate expired/revoked、kill switch、wrong principal | `unauthorized` / `stopped` | 否 | HTTP 前终止；模型不能自我升级。 |
| deterministic risk reject | `rejected` + stable rule code | 否 | 不静默缩单；可在规则允许下做新 proposal。 |
| `live_reviewed` reviewer reject | synthetic rejection to Agent；无 execution intent | 否 | 明确告知不要重试同 call；要修改必须新 proposal/digest。 |
| HITL resume wrong thread/order | graph error | 否 | 不触网；使用原 thread/config，逐 action 对齐 decision。 |
| DB lock/serialization conflict before reservation | `busy` | 可由 application 有界重跑 local transaction | 尚未触网；不能由 model 随意并发重试。 |
| OpenTrade 401/403/config invalid | `blocked_auth` | 否 | terminal operational alert；不把 raw body/token 给模型。 |
| OpenTrade explicit 4xx/risk reject | `rejected_remote` | 否 | 保存 stable/redacted reason；本地 policy 仍不放宽。 |
| OpenTrade explicit accepted | `accepted` + remote id | 否 | 后续 inspect 观察 fill/partial/open。 |
| timeout/connection reset/unparseable response after `SENDING` | `ambiguous` | **绝不** | reservation 保持；只调用 `inspect_trade` 对账 orders/trades/positions。 |
| crash before reservation | 无 intent / 无 network | 可重新 prepare/call | proposal unique 仍生效。 |
| crash after reservation、HTTP 前 | `reserved/sending` | 不直接重发 | recovery 先按 phase 和 remote reads reconcile；必要时标 ambiguous。 |
| crash after venue accepted、receipt commit 前 | `ambiguous` | **绝不** | 用 execution fingerprints、order/trade/position history 对账。 |
| cancel timeout | `ambiguous` | **绝不** | 查 open/closed order；不要盲发第二次 DELETE。 |
| close timeout | `ambiguous` | **绝不** | 查 position/trades；防止二次平仓反向开仓。 |
| model/tool budget exhausted before write | `research_incomplete` | 否 | 不生成新 execution；已有 order 状态不受模型总结影响。 |
| streaming/UI disconnected | 无资金状态变化推断 | 否 | 从 TradingOrderStore 恢复；stream 只做投影。 |

## 8. 推荐 harness 配置

以下是结构示例，不是复制即投产的完整代码；`TradingOrderModule`、persistent saver、tool schemas 和 custom budget middleware 需要实现并做 contract tests。

```python
from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
    ToolCallRequest,
)

TRADE_WRITE_TOOL_NAMES = frozenset(
    {"place_order", "cancel_order", "close_position"}
)
RESEARCH_READ_TOOLS = [
    read_news_case,
    search_primary_sources,
    read_market_snapshot,
    read_instrument_metadata,
]
RESEARCH_READ_TOOL_NAMES = [tool.name for tool in RESEARCH_READ_TOOLS]

MODEL_ID = "openai:gpt-5.5"
register_harness_profile(
    MODEL_ID,
    HarnessProfile(
        excluded_tools={"execute", "write_file", "edit_file", "delete"},
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    ),
)

scratch = StateBackend()

def read_only_filesystem() -> FilesystemMiddleware:
    # A fresh middleware instance per graph/subagent stack; no host shell/files.
    return FilesystemMiddleware(
        backend=scratch,
        tools=["read_file", "ls", "glob", "grep"],
        grep_max_count=200,
    )

def safe_read_error(exc: Exception, request: ToolCallRequest) -> str | None:
    if request.tool_call["name"] in RESEARCH_READ_TOOL_NAMES:
        return f"{request.tool_call['name']} failed with {type(exc).__name__}."
    return None  # Never convert an unexpected trading-write exception to fake success.

def read_middleware() -> list:
    return [
        read_only_filesystem(),
        ModelCallLimitMiddleware(run_limit=10, exit_behavior="error"),
        ToolCallLimitMiddleware(run_limit=20, exit_behavior="error"),
        # Retry is inner/earlier; scope is explicit and contains NO writes.
        ToolRetryMiddleware(
            tools=RESEARCH_READ_TOOL_NAMES,
            retry_on=(TimeoutError, ConnectionError),
            max_retries=2,
            on_failure="error",
        ),
        ToolErrorMiddleware(
            on_error=safe_read_error,
            tools=RESEARCH_READ_TOOL_NAMES,
        ),
    ]

subagents = [
    {
        "name": "fact-verifier",
        "description": "Verify one news event against primary sources and return typed facts.",
        "system_prompt": FACT_VERIFIER_PROMPT,
        "tools": [read_news_case, search_primary_sources],
        "middleware": read_middleware(),
        "response_format": VerifiedEventFacts,
    },
    {
        "name": "market-structure",
        "description": "Resolve tradable instruments and assess liquidity/regime read-only.",
        "system_prompt": MARKET_STRUCTURE_PROMPT,
        "tools": [read_market_snapshot, read_instrument_metadata],
        "middleware": read_middleware(),
        "response_format": MarketAssessment,
    },
]

# Static at graph construction/deployment; never read from model/tool arguments.
STATIC_PROFILE: ExecutionMode = load_operator_owned_execution_profile()
if STATIC_PROFILE == "paper":
    execution_adapter = PaperExecutionAdapter()  # Has no live credential capability.
elif STATIC_PROFILE in {"live_reviewed", "live_bounded"}:
    execution_adapter = OpenTradeExecutionAdapter(secret_manager)
else:
    raise ValueError("unknown static execution profile")

order_module = TradingOrderModule(
    static_profile=STATIC_PROFILE,
    execution_adapter=execution_adapter,
    order_store=trading_order_store,
)
prepare_trade_action = order_module.prepare_trade_action_tool()
inspect_trade = order_module.inspect_trade_tool()
place_order = order_module.place_order_tool()
cancel_order = order_module.cancel_order_tool()
close_position = order_module.close_position_tool()

LIVE_REVIEW_INTERRUPTS = {
    name: {"allowed_decisions": ["approve", "reject"]}
    for name in TRADE_WRITE_TOOL_NAMES
}
review_middleware = (
    [HumanInTheLoopMiddleware(LIVE_REVIEW_INTERRUPTS)]
    if STATIC_PROFILE == "live_reviewed"
    else []
)

agent = create_deep_agent(
    model=explicit_model,
    system_prompt=TRADING_SUPERVISOR_PROMPT,
    tools=[
        *RESEARCH_READ_TOOLS,
        prepare_trade_action,  # Local immutable proposal; no venue write.
        inspect_trade,         # Read/reconcile.
        place_order,           # Paper, or REAL OpenTrade POST /orders in live profiles.
        cancel_order,          # Paper, or REAL OpenTrade DELETE /orders/:id.
        close_position,        # Paper, or REAL OpenTrade POST /positions/close.
    ],
    subagents=subagents,
    backend=scratch,
    middleware=[
        # First in the user list: later after_model guards run before this HITL.
        *review_middleware,
        read_only_filesystem(),  # Replaces the default FS middleware by name.
        SingleTradeWritePerModelMessage(TRADE_WRITE_TOOL_NAMES),
        ModelCallLimitMiddleware(run_limit=20, exit_behavior="error"),
        ToolCallLimitMiddleware(run_limit=40, exit_behavior="error"),
        ToolCallLimitMiddleware(
            tool_name="place_order", run_limit=1, exit_behavior="error"
        ),
        ToolCallLimitMiddleware(
            tool_name="cancel_order", run_limit=1, exit_behavior="error"
        ),
        ToolCallLimitMiddleware(
            tool_name="close_position", run_limit=1, exit_behavior="error"
        ),
        ToolRetryMiddleware(
            tools=RESEARCH_READ_TOOL_NAMES,
            retry_on=(TimeoutError, ConnectionError),
            max_retries=2,
            on_failure="error",
        ),
        ToolErrorMiddleware(
            on_error=safe_read_error,
            tools=RESEARCH_READ_TOOL_NAMES,
        ),
    ],
    context_schema=TradingRunContext,
    response_format=TradingRunSummary,
    checkpointer=postgres_checkpointer,
    store=None,
    name="news-trading-agent-v1",
)
```

启动后必须 programmatically assert：

```text
main contains: place_order, cancel_order, close_position
main excludes: write_file, edit_file, delete, execute
every subagent excludes: place_order, cancel_order, close_position,
                         write_file, edit_file, delete, execute
ToolRetryMiddleware.tools ∩ TRADE_WRITE_TOOL_NAMES == ∅
paper           → PaperExecutionAdapter, no live secret, no HITL middleware
live_reviewed   → OpenTradeExecutionAdapter, all three writes use approve/reject HITL
live_bounded    → OpenTradeExecutionAdapter, no HITL middleware
runtime context profile == graph STATIC_PROFILE == DB mandate profile
```

工具描述必须由静态 profile 生成且直白告诉模型：`paper` 的描述明确写“模拟、不会触达真实 venue”；两个 live profile 才写下面的真实副作用：

- `place_order` 会真实花费资金；只接受 `kind=place` 的 ready proposal；
- `cancel_order` 会真实撤销属于当前 mandate 的活动订单；
- `close_position` 会真实 reduce/close 已有 derivative position，绝不反向开仓；
- `ambiguous` 后唯一安全写法是停止写入并调用 `inspect_trade`。

## 9. `live_bounded` 的真实自主执行示例

初始一次性授权由 operator 创建/启用 mandate，例如：允许的账户、venues/instruments、单笔/累计 notional、gross/net exposure、最大持仓/挂单数、slippage、有效期、loss/drawdown boundary、交易时段和 kill switch。这个授权不是逐单审批；模型也不能创建或修改它。

应用服务先选择已经静态构造为 `live_bounded` 的 graph（其三个 tools 已绑定 `OpenTradeExecutionAdapter`，且没有 HITL middleware），再从 DB 构造同 profile 的 context；不一致则拒绝启动：

```python
runtime_context = TradingRunContext(
    tenant_id="tenant-7",
    principal_id="agent-service",
    mandate_id=mandate_id,
    case_id=case_id,
    agent_execution_id=stable_execution_id,
    execution_mode="live_bounded",  # Resolved from the active mandate, not user text.
)

config = {"configurable": {"thread_id": str(stable_thread_id)}}

result = await agent.ainvoke(
    {
        "messages": [{
            "role": "user",
            "content": (
                "Analyze this news case and the prior 72 hours. Verify primary facts, "
                "resolve an eligible instrument, and trade only if the frozen mandate "
                "and deterministic risk checks permit it. Manage or exit the position "
                "if the thesis invalidates."
            ),
        }]
    },
    config=config,
    context=runtime_context,
    version="v2",
)
```

合法工具链可能是：

```text
task(fact-verifier) + task(market-structure)       read-only
prepare_trade_action(PlaceIntent(...))             proposal P1
place_order(ProposalRef(P1, digest))               actual OpenTrade POST; no HITL
inspect_trade(P1)                                  reconcile/read
prepare_trade_action(CancelIntent(...))            proposal C1
cancel_order(ProposalRef(C1, digest))               actual DELETE; no HITL
prepare_trade_action(CloseIntent(...))             proposal X1
close_position(ProposalRef(X1, digest))             actual close POST; no HITL
```

该 graph 没有 write interrupt，所以每一次 place/cancel/close 都由主 Agent 自己调用并由 Adapter 立即执行；这就是用户要求的真实自主权限。`live_reviewed` graph 的同名 tools 仍绑定真实 Adapter，但三者静态配置 `approve/reject`，会在 HTTP 前暂停；`paper` graph 则以相同 schema 调用无 live secret 的模拟 Adapter。

## 10. Implementation hides

这是一个深 Module：小 Interface 后面隐藏大量易错、经常变化且不应由 caller/model 重复承担的复杂度。

- OpenTrade base URL、auth header、credential refresh/rotation 和 redaction；
- issuer/ticker → eligible venue → exact CCXT symbol 映射；
- spot/swap/contract 区别，OpenTrade 文档要求优先/选择规则；
- metadata precision、min notional、quantity/quoteAmount、limit/trigger 条件；
- position mode 和 mandatory `hedged`；
- derivative close 的 exact positive quantity 和 reduce semantics；
- server-owned account/mandate lookup、policy version、kill switch；
- notional/gross/net/order-count/drawdown/freshness/rate-limit checks；
- 跨 process 的原子 risk reservation 与 locks；
- proposal canonicalization、digest、revision、expiry；
- `tool_call_id` intent identity、proposal dedupe、attempt journal；
- OpenTrade response parsing、stable error taxonomy、secret-safe observability；
- post-submit ambiguity 和 orders/trades/positions reconciliation；
- checkpoint/thread lifecycle 与 order ledger 的隔离；
- paper/fake/replay/live adapters 的一致 contract tests。

删除测试：如果移除 `TradingOrderModule`，每个 Agent tool 都会重新实现 symbol、hedged、quantity、risk、reservation、idempotency、error parsing 和 reconcile；复杂度会泄漏到 prompt/tool schema/调用方。这证明该 Module 具有足够 Depth 和 Leverage。

## 11. Dependency adapters

| 类别 | 依赖 | Seam / Adapter 决策 |
|---|---|---|
| In-process | canonical schemas、digest、proposal/execution state machine、risk math、reservation rules、stable error codes | 直接放在 `TradingOrderModule` Implementation；不为每个纯函数制造 interface。 |
| Local-substitutable | clock、ID factory、TradingOrderRepository、mandate/case repositories、transaction/lock provider、checkpointer | 只在测试价值高的 seam 放窄 port；PostgreSQL 是生产实现，fake clock/repository 用于 crash/expiry/concurrency tests。 |
| Remote-owned | Tracefold News read API/DB view（若与交易 deployable 分进程）、内部 market snapshot service | Typed read adapters；有 timeout/freshness/version，不让远端 DTO 穿透 domain。 |
| True external | OpenTrade CEX API | `OpenTradeExecutionAdapter`：typed `place/cancel/close/inspect`，secret-injected、one-attempt writes、redacted errors。 |
| True external | primary-source/news providers、market data venues | read-only adapters；可以有有界 retry，结果绑定 source URL/hash/timestamp。 |
| True external | LLM provider | 由 Deep Agents/LangChain model adapter 承担；输出不拥有资金状态或授权。 |

credentials 是 Adapter 的私有 Implementation dependency，不是 `TradingRunContext` 或 Agent filesystem 的数据依赖。OpenTrade API 可替换为 future broker Adapter，而五个 Agent-facing tools 和 domain invariants 不变。

## 12. Trade-offs 与未选择方案

### 12.1 方案 A 的收益

- 用户要求得到满足：DeepAgent 真实可自主 place/cancel/close，而不是只提建议。
- 仅三个 venue-write tools，Interface 小；venue 和风险复杂度集中，Locality 高。
- subagent tool scoping 清楚，研究并行不会扩大资金权限。
- 三档共用同一 tool schema 和 `TradingOrderModule` contract，但静态绑定不同 Adapter/HITL；权限差异可在启动时验证，不依赖运行中 predicate。
- proposal ref/digest 让 review、审计、幂等和 replay 绑定同一 exact action。
- Adapter 隐藏 secret 但不剥夺 Agent capability；安全和产品语义没有混淆。

### 12.2 明确代价

- `TradingOrderModule` 内部并不“小”：数据库 locks/reservations、crash windows 和 reconcile 是必要复杂度；KISS 体现在外部 Seam。
- `live_bounded` 接受 LLM 会主动调用资金工具的风险，只能用小额度、窄 universe、短 mandate、kill switch 和完整 canary/eval 控制，不能靠 prompt 消除。
- OpenTrade 的公开 CEX contract 没有 idempotency key，无法诚实宣称 exactly-once；one-attempt + ambiguity 会牺牲部分可用性。
- HITL 会增加 `live_reviewed` latency；但 `live_bounded` 不付逐单延迟，`paper` 也不会触达真实资金。
- `StateBackend` 和无 cross-thread Store 减少 Agent 长期学习便利，换来更干净的权限/事实边界。
- OpenTrade 是否能交易具体 US/KR equities 必须由 metadata 实证，不能因新闻 ticker 存在而假设。

### 12.3 未选择方案

| 方案 | 不选原因 |
|---|---|
| Agent 只生成 proposal，由独立非 Agent orchestrator 下单 | 违背本次明确目标；权限不在 DeepAgent。 |
| 把 OpenTrade 的 generic HTTP/curl/40 endpoints 全给 Agent | Interface 浅、tool sprawl、secret/exfiltration 面大，模型承担 venue quirks；无法 KISS。 |
| 只给一个 `trade(any_payload)` 工具 | 虽然工具更少，但 place/cancel/close 的风险语义、HITL、审计、limits 混在一个 schema，易误路由。 |
| `place_order` 直接接收 exchange/symbol/quantity/hedged/token | 让 LLM 重做 instrument/risk/precision/credential plumbing；Module 没有 Depth。 |
| 默认 GP subagent 继承主 tools，再靠 prompt 禁止下单 | 与 `0.7.8` 源码的真实 tool inheritance 冲突；不是权限控制。 |
| 每个 write 自动 retry 以提高成功率 | post-submit timeout 会造成双单/过度平仓；OpenTrade 公开合同没有 idempotency field。 |
| 只靠 HITL 保障资金安全 | live_bounded 不逐单 HITL，且并发/crash/remote ambiguity 不由 reviewer 解决；必须 DB invariant。 |

## 13. 最小验证与发布门槛

在任何 live_bounded canary 前，至少完成以下自动化验证：

1. **Pinned dependency test**：lock 精确等于研究基线或有显式升级审查；启动打印版本但不打印 secrets。
2. **Tool visibility test**：断言主 Agent 有三个 writes，所有 subagents 无 writes，所有层无 FS write/delete/execute。
3. **Additive-tools regression test**：升级 Deep Agents 后重新检查 built-ins，避免误以为 `tools=` 能移除默认工具。
4. **Default GP regression test**：`GeneralPurposeSubagentProfile(enabled=False)` 生效；任何新 auto subagent 不得继承 writes。
5. **Static profile test**：`paper` 三个 writes 均无 interrupt、只调用无 live secret 的 Paper Adapter；`live_reviewed` 三个 writes 均 interrupt、approve 后才调用 recording OpenTrade Adapter；`live_bounded` 三个 writes 均无 interrupt且直接调用 recording OpenTrade Adapter。unknown/mismatch profile 必须拒绝构造或零 network。
6. **HITL test**：approve 执行原 digest，reject 零 network；edit/respond 不在 allowed decisions；wrong thread/decision order 零 network。
7. **Retry-scope test**：read timeout 可有界 retry；place/cancel/close timeout 各只有一次 Adapter call。
8. **Parallel ToolNode test**：同一 AIMessage 发两个 place calls、place+close、两个 process 并发；原子 reservation/unique constraints 至多允许符合 aggregate mandate 的 sends。
9. **Idempotency test**：同 tool call replay、同 proposal 不同 tool call IDs、HITL resume、worker restart均不二次 send。
10. **Crash matrix**：在 reservation 前、reservation 后、SENDING 前后、HTTP return 前后、receipt commit 前后注入 crash；post-send uncertainty 全部进入 AMBIGUOUS。
11. **Reconcile test**：open/closed orders、trades、positions 的矛盾、partial fill、manual external trade、cancel/close ambiguity都有确定 outcome；无法证明时保持 ambiguous reservation。
12. **Risk fail-closed test**：market data、Redis-equivalent cache、account、position mode、metadata、DB lane 任一 unavailable，place 不触网；不能继承 OpenTrade fail-open。
13. **Mandate test**：expired/revoked、wrong tenant/principal/account、超 notional/exposure/order count/drawdown、禁用 instrument/venue全部零 network；live_bounded 也相同。
14. **Secret test**：prompt、messages、checkpoint、StateBackend files、stream v3、LangSmith trace、exceptions、receipts 中无 bearer token/exchange secret/raw auth headers。
15. **Contract test**：Adapter 依据固定 OpenTrade spec 正确发送 exact CCXT symbol、order type、conditional prices、positive close quantity 和 hedged mode。
16. **Event-study eval**：72 h corpus 顺序回放；必须分别报告 coverage、可交易 instrument coverage、proposal rate、execution simulation、重复 thesis、最大风险占用，不把个别 MRVL/GOOGL/SK Hynix/MRNA 手工案例当普遍胜率。
17. **Canary**：先 recording/`paper` Adapter，再 `live_reviewed` 极小额，再限时限额 `live_bounded`；任何 ambiguous、secret leak、double-send、risk overshoot 均是 hard rollback。

## 14. Primary-source index

### Deep Agents 0.7.8 / LangChain / LangGraph

- [Deep Agents 0.7.8 release](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.8)
- [Pinned Deep Agents tree](https://github.com/langchain-ai/deepagents/tree/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8)
- [Package metadata](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/pyproject.toml)
- [Official resolved uv.lock](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/uv.lock)
- [`create_deep_agent` source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/graph.py)
- [SubAgent source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/middleware/subagents.py)
- [FilesystemMiddleware source](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/deepagents/middleware/filesystem.py)
- [HITL integration tests](https://github.com/langchain-ai/deepagents/blob/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/libs/deepagents/tests/integration_tests/test_hitl.py)
- [Official deep_research example](https://github.com/langchain-ai/deepagents/tree/1e261ba201bb1af4dbc5cbc8b6424e709b850ea8/examples/deep_research)
- [Deep Agents subagents docs](https://docs.langchain.com/oss/python/deepagents/subagents)
- [Deep Agents async subagents docs](https://docs.langchain.com/oss/python/deepagents/async-subagents)
- [Deep Agents HITL docs](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop)
- [Deep Agents backends docs](https://docs.langchain.com/oss/python/deepagents/backends)
- [Deep Agents permissions docs](https://docs.langchain.com/oss/python/deepagents/permissions)
- [Deep Agents customization docs](https://docs.langchain.com/oss/python/deepagents/customization)
- [Deep Agents event streaming docs](https://docs.langchain.com/oss/python/deepagents/event-streaming)
- [LangChain prebuilt middleware docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in)
- [LangGraph persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph functional API determinism/idempotency](https://docs.langchain.com/oss/python/langgraph/functional-api#determinism)
- [Pinned LangGraph ToolNode source (`prebuilt==1.1.0`)](https://github.com/langchain-ai/langgraph/blob/3614e88c58af63f597764218646e85c49952b2da/libs/prebuilt/langgraph/prebuilt/tool_node.py)
- [Pinned LangChain HITL source (`langchain==1.3.15`)](https://github.com/langchain-ai/langchain/blob/f4bc5031dbcf24edb0374a07830915a285222567/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py)

### OpenTrade

- [Pinned OpenTrade tree](https://github.com/6551Team/opentrade/tree/0efa9b4d27fc644a667453c5c41e55ad0d04557d)
- [OpenTrade CEX skill](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md)
- [Place Order](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L1264-L1388)
- [Cancel Order](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L1392-L1418)
- [Close Position](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L1628-L1669)
- [Risk Engine Notes](https://github.com/6551Team/opentrade/blob/0efa9b4d27fc644a667453c5c41e55ad0d04557d/opentrade-cex/SKILL.md#L2132-L2152)

## 最终架构决定建议

若目标确实是“DeepAgent 有真实下单权限”，建议 Issue 采用方案 A，并把下面一句作为不可模糊的验收语义：

> 在 `live_bounded` mandate 下，主 DeepAgent 自身可直接调用 `place_order`、`cancel_order`、`close_position`，这些 tools 使用真实 OpenTrade credential capability 立即触发真实 CEX write endpoint，不需要逐单人工审批或独立非 Agent execution orchestrator；同时所有 subagents 无交易写权限，所有交易写入受 server-owned mandate、数据库原子风险 reservation、one-attempt/AMBIGUOUS reconciliation 和 kill switch 约束。

这既保留 Agent 的真实行动能力，也把不可交给概率模型的复杂度收进一个有 Depth、可测试、可替换的订单 Module。
