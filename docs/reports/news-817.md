# #817：非通知版本的有效决定与重复来源

[Issue](https://github.com/AnalyThothAI/tracefold/issues/817) · [视觉稿](https://claude.ai/artifact/NeQk8qick4NwKSfYeLcrVB) · [详情设计](../design/news-event-detail.md)

本报告记录实现分支的只读验收；合并与镜像发布回执保存在实现 PR。

## 同快照逐条核对

2026-10-03 17:17:47 UTC，在生产 PostgreSQL 的 `REPEATABLE READ, READ ONLY` 事务内核对此前 24 小时。基线为 `666c80290`，新投影读取同一批持久事实；没有模型调用、通知重规划或数据写入。

| 标签 | 改动前 | 改动后 |
| --- | ---: | ---: |
| 全部 | 220 | 220 |
| 已推送 | 70 | 70 |
| 未推送 | 150 | 150 |
| 处理中 | 0 | 0 |

220/220 条列表与详情的完整 outcome 一致；旧的“没有待执行通知 / 当前没有未完成通知责任”出现 0 次。旧快照的 247 条已随时间滚动，故使用 Issue 允许的新 24 小时窗口。

| 原始事件 | 实际读取结果 |
| --- | --- |
| `4edfb0be`，CBS | 重复；原事实 03:08 UTC 已出现，原条未推送，原因为“旧版模型判断：只进信息流”；详情可打开原条 |
| `3c88a2d3`，Vistra | 重复；读者 10-02 19:21 UTC 已收到同一事实，仍不单独推送 |
| `ec12db11`，房贷 | 沿用 13:26 UTC 判断；推送概率 26%、推送线 37%；新增来源后没有新决定 |
| `d20f7b6f`，英伟达回购 | 保留 13:27 UTC 送达中文标题、冻结正文和两件已推送事实；之后新增 1 个来源 |
| `629f62c3`，敖德萨 | 标题事实概率 12% / 推送线 37%；另一个事实明确显示首次出现已超过 3 小时 |

这些事件的 `news why` 与详情 outcome kind 相同。浏览器按本地时区显示结构化时间，CLI 明示 UTC。报告里的 UTC 时刻与视觉稿 UTC+8 相差八小时。

## 首页计划与时延

在上述同一只读快照内，对真实首页“51 行分页 + 标签计数”语句执行 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`。先预热一次，再取五次执行时间中位数：基线 **47.128 ms**，新投影 **50.902 ms**，新增读取成本 **3.774 ms**。

原分页与计数访问路径保留。新增原条解析位于 `selected AS MATERIALIZED` 分页之后，只在无 notify 工作的采用行执行；真实计划使用 `news_analyses_update_ref` 和 `news_notifications_sent_claims`，没有为重复解析增加全窗口扫描。旧决定文档仅在完成工作确实停在旧版本时读取，通过 `news_analyses_event_revision_key` 定位。

源差集按 publisher / artifact 身份计算，而非报道记录数；同一原文的转载记录不会被误算为新来源。生产逐条核对也覆盖了这类记录。

## 视觉与交互证据

预览使用本分支 React 和上述真实数据库详情，经 HTTP 资产映射及公开 schema 校验；工作台其余接口来自现有只读 Serve。浅蓝色“改动标记”属于视觉稿注释，不作为产品样式。工作台外壳、原文、故事、工程折叠和行情入口继续使用现有页面。

| 页面 | 桌面 | 手机 |
| --- | --- | --- |
| 未推送事件流 | [截图](../design/news-817-feed-desktop.jpg) | [截图](../design/news-817-feed-mobile.jpg) |
| 重复原条 | [截图](../design/news-817-duplicate-desktop.jpg) | [截图](../design/news-817-duplicate-mobile.jpg) |
| 未推送后新增来源 | [截图](../design/news-817-silent-desktop.jpg) | [截图](../design/news-817-silent-mobile.jpg) |
| 已推送后新增来源 | [截图](../design/news-817-sent-desktop.jpg) | [截图](../design/news-817-sent-mobile.jpg) |

验证覆盖纯投影、真实 PostgreSQL 装配、OpenAPI 生成、组件与浏览器四视口。额外核对旧工作重新 pending 不沿用、矛盾通知版本归入未推送诊断、原条和重复事件都继续新增来源后仍能定位原始采用版本。具体命令和最终 CI 结果见实现 PR。
