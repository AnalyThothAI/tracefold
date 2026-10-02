# #791 PR-A：统一命题召回验收进度

[Issue #791](https://github.com/AnalyThothAI/tracefold/issues/791) · [PR #794](https://github.com/AnalyThothAI/tracefold/pull/794) · [News 手册](../modules/news.md)

**A 仍为草稿：工程缺陷已持续修复，召回污染及读者误判的业务验收尚未完成。** PR-C #792 已合并部署，并在真实断线补抄后验证新鲜稿件进入语义处理。B #796 与 A 分开验证；本报告不关闭 #791。

Owner 已批准离线模型重问、标注与校准，并授权业务完成后合并、部署和线上检查。延迟指标按后续指示只作观察；完整窗口、精确版本、事务外模型调用、取消与降级仍是功能正确性要求。

## 当前实现

- `prepare_rank()` / `rank()` 是 prior、冻结回执与离线重放的唯一融合实现；精确向量窗口、PostgreSQL FTS 和同源路线共用 RRF。候选验证在最终名额截断之前。回执按最佳冻结命题聚合，真实已链接回执优先。
- 语义领取事务只读取本 Event 与同源目标；抽取、嵌入之后才读取跨 Event prior。本 Event 内仍全对判断。reader context 只召回一次，发送 CAS 核对已送集合与链接图世代。
- `news_claim_index` 保存 `(claim_ref, text_sha256)`、完整模型身份和 fp16 向量。采用写入缺向量的持久行；Janitor 独立循环持续补算有界批次，避免每分钟只处理一批无法追上新稿。模型调用在事务外，故障时保留待补事实并继续维护。
- 稠密输入保持 `claim_embed_text_v1` 的 statement。FTS 使用唯一 `claim_lexical_text_v1`：statement、subject、action、object、speaker、quantities 的 name / unit / value / period。查询、生成列和未入索引的冻结回执都用同一投影，不补写别名。
- 私密独立配置 `llm.news_embedding` 包括密钥文件、端点、模型和批次。应用镜像不含 Torch 或模型权重；可选独立服务提供严格身份、顺序和维度协议。每批响应核对模型和完整嵌入器身份，避免同一端点更换模型后按旧身份存向量。
- 稠密自检或外部批次失败只关闭该路线，继续 FTS 与同源召回。临时启动故障 30 秒后重试。`recall_dense=on` 同时要求新鲜 Workers 心跳、路由验证成功和活动索引无待补项。
- 0426 创建新索引表并删除 0419 的检索列、函数、trigram 索引；完整替换旧 DF、结构与三通道路径。无兼容 alias。当前实际领域表数量是 27 → 28，Issue 的 26 → 27 早于 reader clock。

## 质量证据与未完成项

原审计材料曾存于 `/tmp`，WSL 重启清除了原始数值数组和重问 journal。此前观察到的失败仍作为失败记录保留；不能把摘要当作当前代码或最终模型的通过证明。[历史 MiniLM 种子实验](issue-791-claim-recall-2026-10-02.json)仅保存旧候选结果。

已从持久审计日志恢复原 150 条查询及全部 10,394 单元，并逐一绑定冻结事实，0 缺失、0 歧义。原审计明确约定默认 U；该约定只适用于全部读过的原候选池，池外新候选保持 unknown。重新导出的原 A 窗口与 B 的业务窗口分开记录；完整 snapshots 保留 query-time head、mask、来源与冻结 sent_claims，禁止以 claim ref 最早版本或今天的 head 代替当时事实。

此前候选均未满足联合约束：

| 失败候选或重问 | 已观察结果 | 结论 |
| --- | --- | --- |
| MiniLM 完整 slot 重放 | 比较对 14,085 → 7,551；独立 50 查询中 171/186 为同话题或无关 | 减量达到目标，但污染未通过 |
| BGE 候选 | SF 回执 31/32；已知重复 20/20；独立 50 查询中 27/33 为同话题 | 召回不能替代上下文精度 |
| Qwen 候选 | 没有找到同时满足总体与俄文 SF 的 prior 配置 | 不作为胜出模型 |
| 原生 A-v2 读者重问 | 206 个变更上下文、412 回答；20 个重复不再重点；另审 30 条有 2 误锚、3 新细节误降、0 feed 误升级 | 新事实与误锚仍需修复并重新证明 |

当前重新编码与校准复用恢复的原金标和实际生产核心，保留完整模型 / 文本 / 数组 / 顺序键摘要。只有同时满足 SF、语种、降级、20 重复链、50 查询污染和完整 24 h 比较对约束，才更新正式校准。最终模型还须提供真实固定多语言及长文本截断探针，并通过部署服务的数值自检；不能以相同维度或弱翻译排序冒充一致的包装参数。

A 的合并前剩余业务证明为：最终配置联合召回验收、#750/#755 回归、完整 24 h 只读重放、读者重点重复抑制、新事实不降级、误锚 ≤1、feed 不误升级。正式选型后的 CI、模型服务加载、上线缺向量补算和真实线上回执也必须重新核实。

## 已执行工程检查

提交 `4ca84d5c8` 的 [CI 36994911255](https://github.com/AnalyThothAI/tracefold/actions/runs/36994911255) 七项全部成功：static、hermetic、PostgreSQL、broker、frontend、deploy-e2e、ci-gate。后续 FTS / 身份 / 数值自检改动仍需其自身最终 HEAD 的 CI；不把前一提交结果外推。

| 本地检查 | 实际结果 | 范围 |
| --- | --- | --- |
| `make check` | 349 passed，4 deselected；static 通过 | 工程修复及 canonical FTS 阶段 |
| News 单元集合 | 1,415 passed | canonical FTS、逐批身份及真实固定向量自检阶段 |
| News / app 嵌入器类型检查 | 150 文件通过 | 生产接口与字段投影 |
| 相关 PostgreSQL 集合 | 44 passed，1 slow deselected | 精确版本、冻结 FTS、迁移 head、schema、模型 deadline / 降级；固定向量替换后的 deadline 另 1 passed，1 slow deselected |
| 服务与补算 focused 集合 | 54 passed | 真 HTTP 协议、故障、取消、顺序、维护独立性 |
| 独立 CPU 容器 | 固定 revision 实际加载；应用黄金自检与 EN / ZH / RU 请求通过 | 真实镜像、0600 密钥、非 root / read-only；自检 0.564 s 只作观察，非正式选型验收 |

各集合有重叠，不相加。两个本地部署 shell 用例因用户保留的 PostgreSQL 初始化脚本 CRLF 失败；使用 Git HEAD 原脚本在隔离目录中复测通过。该用户文件没有修改或提交。

真实模型启动发现并修复了 ST 6.1 pooling 属性检查漂移：改读当前 `get_config_dict()`，删除旧属性检查及已弃用导入，不留兼容分支。服务重新构建、实际启动与应用黄金自检已通过。固定探针错 cap 128 / 512、错 CLS 均偏离容差；正确单条 / chunk2 / batch6 的最大分量差约 1.2e-7。wheel 中模型身份与黄金资源字节匹配源码。

独立 CUDA profile 只让模型看到物理 GPU 1；当前宿主缺容器 GPU runtime，真实 Docker probe 被拒绝。CPU 运行路径可用，但真实胜出模型的内存、加载和请求必须实测，不以 Compose 渲染成功代替运行。

生产规模 slow 测量仍保留 17,500 个当前命题及 2,000 个超过 7 d 的已送冻结版本。此前 p95 prior 489.019 ms / receipt 140.150 ms，未达到原 200 / 80 ms 数字；遵照 owner 后续指示只作观察，不据此阻止业务完成后的交付。
