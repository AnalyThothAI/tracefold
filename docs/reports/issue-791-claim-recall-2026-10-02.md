# #791 PR-A：统一命题召回验收进度

[Issue #791](https://github.com/AnalyThothAI/tracefold/issues/791) · [PR #794](https://github.com/AnalyThothAI/tracefold/pull/794) · [News 手册](../modules/news.md)

**A 已选择简单最终配置，等待最终提交 CI 后与 B 联合合并部署。** Owner 接受以下明确误差；没有宣称全部原研究门槛通过。PR-C #792 已合并部署，并在真实断线补抄后验证新鲜稿件进入语义处理。本报告不关闭 #791。

Owner 已批准离线模型重问、标注与校准，并授权业务完成后合并、部署和线上检查。延迟指标按后续指示只作观察；完整窗口、精确版本、事务外模型调用、取消与降级仍是功能正确性要求。

Owner 随后再次明确允许合理误差，要求避免过度修复和不必要复杂度。收尾采用原共享 RRF 的简单配置，停止添加资格学习器及为达到全部旧数字而反复扩展提示词。旧验收指标保留为对照；交付以必要功能、真实 bug 回归、代表性业务重放及当前提交 CI 为依据，并明示实际误差。

## 当前实现

- `prepare_rank()` / `rank()` 是 prior、冻结回执与离线重放的唯一融合实现；精确向量窗口、PostgreSQL FTS 和同源路线共用 RRF。候选验证在最终名额截断之前。回执按最佳冻结命题聚合，真实已链接回执优先。
- 语义领取事务只读取本 Event 与同源目标；抽取、嵌入之后才读取跨 Event prior。本 Event 内仍全对判断。reader context 只召回一次，发送 CAS 核对已送集合与链接图世代。
- `news_claim_index` 保存 `(claim_ref, text_sha256)`、完整模型身份和 fp16 向量。采用写入缺向量的持久行；Janitor 独立循环持续补算有界批次，避免每分钟只处理一批无法追上新稿。模型调用在事务外，故障时保留待补事实并继续维护。
- 稠密输入保持 `claim_embed_text_v1` 的 statement。FTS 使用唯一 `claim_lexical_text_v1`：statement、subject、action、object、speaker、quantities 的 name / unit / value / period。查询、生成列和未入索引的冻结回执都用同一投影，不补写别名。
- 私密独立配置 `llm.news_embedding` 包括密钥文件、端点、模型和批次。应用镜像不含 Torch 或模型权重；可选独立服务提供严格身份、顺序和维度协议。每批响应核对模型和完整嵌入器身份，避免同一端点更换模型后按旧身份存向量。
- 稠密自检或外部批次失败只关闭该路线，继续 FTS 与同源召回。临时启动故障 30 秒后重试。`recall_dense=on` 同时要求新鲜 Workers 心跳、路由验证成功和活动索引无待补项。
- 0426 创建新索引表并删除 0419 的检索列、函数、trigram 索引；完整替换旧 DF、结构与三通道路径。无兼容 alias。当前实际领域表数量是 27 → 28，Issue 的 26 → 27 早于 reader clock。

## 最终配置与质量证据

[最终机器可读证据](issue-791-claim-recall-release-2026-10-02.json)固定模型、配置、原始排名缓存与实际最终选择的 SHA。生产选择保持 MiniLM 固定 revision `e8f8c211226b894fcb81acc59f3b34ba3efd5f42`，384 维、256 token、mean pooling、float32 / L2。prior k=5、dense floor=.6；receipt k=16、dense floor=.4；两者 lexical floor=.9、degraded floor=.2；RRF 与路线保持原机制。

150 条查询的 prior SF 为 59/65，中文 18/18、俄文 3/3；已知 baseline SF 保留 25/28。实际最终 receipt SF 为 31/32，重复链 19/20，receipt 共 1,204 条，原 baseline 605 条。prior 总关系对 483/771，减少 37.4%，只证明该 150 条样本，不代表完整 24 h。尝试仅收紧 k 后，#750 已知四条真实 gold 消息在 k5 / k9 / k12 只能保留 1 / 2 / 3 条；原 k16 保留四条且无 label0 消息。因此最终保留原 receipt k16，优先确保已知业务回归，没有删除断言或新增算法。

独立完整消息盲评针对 k12 的 50 条实际候选：SF 3、SD 2、TO 31、U 14，无关 90%。这不是 k16 的新抽样，不证明最终配置语义精度已改善，也不能将候选噪声直接解释为最终误推或误锚率。剩余重复漏召回、baseline SF 损失和上下文噪声均保留为明确局限；B 的真实读者回归提供最终判断证据。Owner 允许合理误差，停止学习器、额外资格规则和反复切点实验。

## 历史与证明边界

原审计材料曾存于 `/tmp`，WSL 重启清除了原始数值数组和重问 journal。此前观察到的失败仍作为失败记录保留；不能把摘要当作当前代码或最终模型的通过证明。[历史 MiniLM 种子实验](issue-791-claim-recall-2026-10-02.json)仅保存旧候选结果。

已从持久审计日志恢复原 150 条查询及全部 10,394 单元，并逐一绑定冻结事实，0 缺失、0 歧义。原审计明确约定默认 U；该约定只适用于全部读过的原候选池，池外新候选保持 unknown。重新导出的原 A 窗口与 B 的业务窗口分开记录；完整 snapshots 保留 query-time head、mask、来源与冻结 sent_claims，禁止以 claim ref 最早版本或今天的 head 代替当时事实。

原审计单元的命题和卡片摘要存在固定长度截断；恢复的是原来实际读过的候选池，不能替代对新增完整消息的独立盲评。150 条读者输入已全部绑定，prior / receipt SF 查询分母为 65 / 32，缺失 0。回执主指标现在统计实际链接优先后的最终 m1..m16，raw rank 只作诊断；总关系对的减量同时计入本 Event 和跨 Event。历史 prior 没有独立调用时间，分析完成时间是重放上界，可能包括比较期间才采用的 head；回执的原决策开始时间可精确恢复。报告明确区分这两种时间证明。

此前候选均未满足联合约束：

| 失败候选或重问 | 已观察结果 | 结论 |
| --- | --- | --- |
| MiniLM 完整 slot 重放 | 比较对 14,085 → 7,551；独立 50 查询中 171/186 为同话题或无关 | 减量达到目标，但污染未通过 |
| BGE 候选 | SF 回执 31/32；已知重复 20/20；独立 50 查询中 27/33 为同话题 | 召回不能替代上下文精度 |
| Qwen 候选 | 没有找到同时满足总体与俄文 SF 的 prior 配置 | 不作为胜出模型 |
| 原生 A-v2 读者重问 | 206 个变更上下文、412 回答；20 个重复不再重点；另审 30 条有 2 误锚、3 新细节误降、0 feed 误升级 | 新事实与误锚仍需修复并重新证明 |

当前重新编码与校准复用恢复的原金标和实际生产核心，保留完整模型 / 文本 / 数组 / 顺序键摘要。SF、语种、降级、重复链、消息污染和完整 24 h 比较对继续报告；按 Owner 的最新要求，允许合理误差，不为同时满足全部旧阈值引入额外组件。最终模型仍须提供真实固定多语言及长文本截断探针，并通过部署服务的数值自检。

A 的收尾范围为：简单最终配置的代表性重放、已知重复及错误锚点回归、新重要细节的判断，以及正式提交 CI。部署后检查模型服务、缺向量补算和真实线上处理。剩余误差与历史证据限制明确记录。

## 已执行工程检查

提交 `4ca84d5c8` 的 [CI 36994911255](https://github.com/AnalyThothAI/tracefold/actions/runs/36994911255) 七项全部成功。提交 `3bcf17fdf` 的 [CI 36997937247](https://github.com/AnalyThothAI/tracefold/actions/runs/36997937247) static、hermetic、broker、frontend、deploy-e2e 成功；PostgreSQL 580 passed / 2 failed。两处合成矿山消息只替换 statement 和 subject，却仍继承关税 action，新 canonical FTS 如实召回了错误字段。修复样本的 action / object / 类别后该完整文件 66 passed，排除无关消息和冻结版本断言均保留。提交 `d685c46ee` 的 [CI 36999411386](https://github.com/AnalyThothAI/tracefold/actions/runs/36999411386) 七项全部成功。最终配置提交仍须其自身 HEAD 的 CI，不把本地修复或前一提交结果外推。

| 本地检查 | 实际结果 | 范围 |
| --- | --- | --- |
| `make check` | 349 passed，4 deselected；static 通过 | 工程修复及 canonical FTS 阶段 |
| News 单元集合 | 1,415 passed | canonical FTS、逐批身份及真实固定向量自检阶段 |
| News / app 嵌入器类型检查 | 150 文件通过 | 生产接口与字段投影 |
| 相关 PostgreSQL 集合 | 44 passed，1 slow deselected | 精确版本、冻结 FTS、迁移 head、schema、模型 deadline / 降级；固定向量替换后的 deadline 另 1 passed，1 slow deselected |
| 服务与补算 focused 集合 | 54 passed | 真 HTTP 协议、故障、取消、顺序、维护独立性 |
| 严格离线评测 focused 集合 | 13 passed | 完整身份与摘要、当时 head / mask / 冻结版本、池外 unknown、缺失输入、实际最终消息与总关系对；包含真实 TEMP PostgreSQL FTS |
| 独立 CPU 容器 | 固定 revision 实际加载；应用黄金自检与 EN / ZH / RU 请求通过 | 真实镜像、0600 密钥、非 root / read-only；自检 0.564 s 只作观察，非正式选型验收 |

各集合有重叠，不相加。两个本地部署 shell 用例因用户保留的 PostgreSQL 初始化脚本 CRLF 失败；使用 Git HEAD 原脚本在隔离目录中复测通过。该用户文件没有修改或提交。

真实模型启动发现并修复了 ST 6.1 pooling 属性检查漂移：改读当前 `get_config_dict()`，删除旧属性检查及已弃用导入，不留兼容分支。服务重新构建、实际启动与应用黄金自检已通过。固定探针错 cap 128 / 512、错 CLS 均偏离容差；正确单条 / chunk2 / batch6 的最大分量差约 1.2e-7。wheel 中模型身份与黄金资源字节匹配源码。

独立 CUDA profile 只让模型看到物理 GPU 1；当前宿主缺容器 GPU runtime，真实 Docker probe 被拒绝。CPU 运行路径可用，但真实胜出模型的内存、加载和请求必须实测，不以 Compose 渲染成功代替运行。

生产规模 slow 测量仍保留 17,500 个当前命题及 2,000 个超过 7 d 的已送冻结版本。此前 p95 prior 489.019 ms / receipt 140.150 ms，未达到原 200 / 80 ms 数字；遵照 owner 后续指示只作观察，不据此阻止业务完成后的交付。
