# News EventUpdate 核心与硬切换边界

关联 #706，唯一实现 PR 为 #711；#710 的平行实现路线由本 PR 取代，不复制两套领域类型或历史兼容层。

**状态：核心代码与链路设计草案，尚未生产接线，不可据此关闭 #706 或部署。** 本次不提供或执行数据库迁移，不调用真实模型、通知或交易，不执行测试、构建或部署。新核心不自动转换旧 judgment/taxonomy，不双写旧四轴，也不运行新旧两个分析器。

## 调用链

```text
既有来源接入（typed market facts 继续使用既有旁路）
    ↓
NewsAdmission：来源记录 + 正文/归因修订幂等
    near 只召回候选，不决定等价，不结束语义工作
    ↓
FrozenInput：原文证据、候选命题、合法读取目标
    ↓
NewsAgent / SemanticAnalyzer
    抽取 checkpoint → 所选判断后端 → 语义 observation
    命题、phase、时间、归因、证据关系与 changes
    ↓
NewsStore.atomic_adopt（生产适配尚未实现）
    短事务：head CAS + EventUpdate + public outbox + notification_pending
    ├──────────────────────────────────────┐
    ↓                                      ↓
Notifications                          PublicRelay
    实际已送正文覆盖                       在目标选择之前分流
    内容选择与明确原因                     ├─ catalyst_delta → 既有候选研究
    稳定 intent                            └─ source_update → 相关研究依据修订
    按需生成中文卡片                       接收与幂等由 App/Trading 适配实现
    冻结正文 → 发送前检查 → 实际回执
```

图中的持久化和执行节点表达接口要求，不表示 PostgreSQL、旧 worker 或 Trading 已接通。一个 Agent 是现有进程内的业务编排，不是新增服务、DAG 平台、图数据库或影子分析系统。模型没有写库、发送或下单工具。

## 源码职责

| 文件 | 职责 |
|---|---|
| `tracefold/news/updates/admission.py` | 来源正文修订及候选召回接口；精确重传与近似文本分开。 |
| `contracts.py`、`identity.py` | 新精确合同、证据和命题引用、内容及通知身份。 |
| `semantics.py` | 抽取、关系接续、引用检查与内容组装，不读取读者卡片决定事实。 |
| `judgment.py`、`dspy_backend.py` | 生成式默认路径、可选原生 Choice/Noul 批次、局部降级、共享预算。 |
| `topics.py` | 必要 IPTC 导航词表，不含旧四轴最终语义 owner。 |
| `notification.py` | 真实送达正文覆盖、命题选择、稳定 intent 与冻结卡片。 |
| `public.py`、`service.py` | 公开内容投影、语义/通知/公开接续、有限补证和 repair 回调。 |
| `ports.py` | 持久化和副作用接口合同，不是生产数据库实现。 |
| `tracefold/app/news_updates.py` | 显式组合新核心与独立 News Jev 连接，未接入现有 worker 构造。 |

表中未写完整路径的文件均位于 `tracefold/news/updates/`。

## 命题、内容与时间

来源记录身份与正文修订身份分开。同一来源 ID 的正文或归因实质变化必须推进证据修订；精确重传不刷新首次可用时间。

EventUpdate 容纳多个命题。行动 phase 与 effective_at、发生时间和统计期分离；日期到达不自动证明执行。决定、承诺、条件威胁、具体指引分别表达，不再由单个 statement 标签全量过滤。

稳定 claim 引用属于 Event，不建立全球命题图。声明正文是解释性投影，不能独自决定新的业务 revision。真正的新动作可引用前一命题；数值和阶段同时改变时保留各自变化。等价识别仍依赖模型和召回，不能从 hash 的存在推导语义永远正确。

证据引用携带实际来源、原文和归因。引用跨度存在不等于证据支持该命题；同源转载不等于独立确认。一手声明可以证明声明行为，不自动证实其中对第三方的断言。

首次可用时间、模型完成时间和采用时间分开保存。更换模型、改卡片措辞或重试不应使旧事实重新获得交易时效。

## 判断后端与失败边界

未配置 Jev 时，生成式抽取可融合命题状态、主题、关系和支持判断；只为缺少的必要关系补问。配置 Jev 后，生成式提取开放文字和引用，原生后端承担窄判断。

原生 adapter 使用顶层 Choice/Noul，每个输出字段的 description 指向对应 `inputs.items[n].payload`。批大小仅是打包预算，尾部命题继续分批处理；不使用嵌套 list[Choice]，不手写第二套 HTTP 或概率 decoder。

成功判断通过注入的缓存保存；瞬时失败最多一次当前批次的匹配生成式 fallback，不重跑成功批次，不进行双模型投票。生成式 fallback 使用自己的 Signature，不是把聊天 LM 临时绑定到原生概率题。

原生批次共享第一次进入 Jev 阶段后开始的预算，不逐批重置两秒。业务阶段还有总截止。取消、鉴权/配置错误和编程错误不吞成无新闻价值。概率只保留为判断证据，不相乘成整体正确率，不引入通用置信度放行门。

`impact_channel` 目前仅有窄任务合同，尚未接入独立的解释校验分支。implications 仍为抽取结果中的显式条件推断；不应把任务名存在写成该能力已完成。

## 通知与发送

Planner 的覆盖候选只能来自同频道实际 sent 正文。系统观察过的报道、未发送草稿和 ambiguous 发送不算读者已知。只有 full 覆盖才据此抑制；partial、unresolved、provider unavailable 不等于 full。标题相似、同故事计数、缺 ticker 和重要性总分不在新 Planner 中形成独立否决。

sending/ambiguous 的重叠命题单独标为 blocked，避免更换 intent 后盲重发；它们不是覆盖证明。其他可通知命题可继续选择，未决部分通过 deferred_claim_refs 保持 pending。

CardComposer 只读取所选命题及其引用，输出中文标题和对应段落。发送器必须发送冻结正文，不能静默裁剪或改写后仍确认原 payload hash。卡片失败只属于同一 intent 的通知工作，不撤销语义或阻断公开 outbox。

进入 sending 后的未知异常不能推导为未发送，需保存 ambiguous 并沿已有确认机制处理。租约、发送幂等和实际 receipt 是副作用正确性约束，不是新闻内容审批。

## 公开契约与 Trading

PublicUpdate 提供结构化 claims/evidence/changes 以及带出处的确定性文本，不依赖 ReaderCard。

PublicRelay 在目标选择之前分派 source_update 与 catalyst_delta。source_update 必须携带前一内容引用及受影响命题引用；接收成功而 News ack 之前退出时，重投保持同一 update_id。

当前只实现公开投影、分流和接收接口，**尚未实现 Trading 数据库中的研究依据修订**。真正的 App/Trading 适配必须只更新引用相关命题的研究，不创建新 Case、不刷新原 TTL、不整体废止同 Event 的无关研究、不隐式撤单或扩大执行权限。原 FrameReader、qualification 和 trading_analysis 仍需切换。

## 有限补证与恢复

已采用语义先提交。仅当存在影响理解的缺口与合法既有读取目标时，才选择额外读取。预算归属持久 lineage，最多一次，重试或补读的新 revision 不复位。不允许模型生成任意 URL 或工具。

返回材料形成针对受影响命题的窄输入；无材料或预算耗尽保留已采用内容。Repair 只将持久 pending 交给现有调度回调，不新增守护进程或消息队列。pending 查询、唤醒和原子预算需要生产存储适配。

## 持久化与副作用合同：无迁移、无 DDL

`ports.py` 的 Protocol 不能替代数据库并发实现；不要求每个对象独立建表，也不允许用 `first:v2` 伪装稳定 intent。

| 边界 | 实际适配必须满足 |
|---|---|
| 来源修订 | 原始证据与待处理标记同事务，保留第一次可用时间。 |
| checkpoint / observation | insert-only；并发重试返回首个保存结果；同 ID 内容冲突明确报错，完成时钟不重写。 |
| atomic_adopt | 比较采用 head，不仅因新证据到达而丢弃已算观测；head 不倒退；采用、outbox、notification_pending 同事务。 |
| atomic_record_plan | 校验 head/reader revision；决定与唯一 intent 同事务；只清理对应完成工作，未决命题继续 pending。 |
| atomic_begin_send | 校验 lease、head、reader 和重叠未明发送；只替换未发送选择，不改 sending 正文。 |
| settle_send | 实际正文/hash、目标、消息 ID、结果和时间同事务；not_sent 重试同一身份，ambiguous 不盲重发。 |
| Trading 接收 | update_id 幂等与相关研究修订同事务；之后再由 News ack。 |
| 额外读取 | lineage 级 durable reservation；新 revision 和 worker retry 不重置预算。 |

本次没有 PostgreSQL、Sender 或研究依据修订的生产实现，没有执行 schema 检查。不得为弥补这些缺口在线建表、绕过旧约束、双写旧格式或使用内存状态冒充持久恢复。

## 已替换与待删除边界

已经替换 `news/program/artifact_tool.py`：删除历史 schema 白名单、旧 image 校验器及旧 registry 先行校验依赖，改为构造并验证新 image、原子发布 registry，之后清理过时的根 image。仅清理内容寻址根文件，不动无关 JSON 或 candidate 子目录；清理失败不回滚合法的新 registry。相应产物切换测试源码同步替换。

**未运行生成器，所以没有实际删除已打包的旧 image；未访问或删除数据库历史事实。** 原 #711 的 SystemOne 请求标识与错误回执修复保留。

以下为同一 PR 的待完成清单，不是已经删除的路径：

| 旧路径/职责 | 切换时一并处理 |
|---|---|
| `news/program/module.py`、`signatures.py`、`runtime.py`、`seed.py` 旧三 Predictor 图 | 新语义 owner、程序加载/身份/学习消费者；删除 taxonomy.primary/fallback。 |
| `news/program/contracts.py` 旧语义/卡片 envelope 与自动适配 | HTTP/schema、recording、learning/review；旧记录不伪装成新 claims。 |
| `news/taxonomy.py` 四轴 owner | 必要 IPTC 导航、真实来源辅助、badge、过滤、API/UI 与学习指标一起收敛。 |
| `news/progression_review.py` 和 `news/program/progression_review.py` | 由采用 changes/关系承接，迁出仍有用的纯展示辅助，删除重复模型复核。 |
| `news/pipeline/triage.py` 与旧 route/reask | 原 broker/capability 接线、存储原子采用、pending repair；不同时运行两套程序。 |
| `news/triage_rules.py` 的旧内容否决 | 同步删除相应 policy/config/UI/测试；不留下无读者旋钮。 |
| `news/pipeline/admission.py` 的 near 终止权力 | 来源正文修订、候选召回、唤醒与精确幂等一起替换，保留 typed market facts。 |
| `delivery.py` 每 Event 一张 first 卡假设 | intent、queue、receipt、preflight、重试和 ambiguous 恢复一起接线。 |
| `app/learning_runtime.py`、`app/workers/wiring/news.py` | 显式 News 连接生命周期、调用回执、配置槽、CLI、capability 和既有市场旁路。 |
| `news/storage/trade_projection.py`、`app/trading_analysis.py` | 新公开契约、Trading 幂等接收、命题关联、FrameReader 与 qualification。 |
| HTTP schemas / News detail / Timeline / labels | 新内容、来源分歧与处理状态投影，API/TS 生成物同次更新。 |

旧实际数据不自动升级为新业务事实。后续 Console 应明确选择只展示新结构化记录，或提供独立原始记录查看器；后者不能成为旧业务执行入口。本次未实现该展示选择。

直接删除仍被 worker 或消费者引用的文件会造成断裂，而不是完成 KISS 硬切换。生产接线和上述消费者清理仍在 #706/#711 跟踪，不另拆重复 PR。

## 验证状态

源码包上一轮仅做过 Python AST 文本解析。本次提交前核对文件内容和 Git 身份，不导入或运行项目代码。测试源码已提供但未运行；未执行 pytest、类型检查、构建、产物生成、数据库迁移、模型请求、通知、交易或部署。旧 #710 的测试记录只属于其原提交，不是本 PR 当前代码的验证结果。
