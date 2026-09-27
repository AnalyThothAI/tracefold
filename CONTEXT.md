# Tracefold 术语与事实边界

[项目首页](README.md) · [中文手册](docs/README.md) · [新闻](docs/modules/news.md) · [复核](docs/modules/review.md)

Tracefold 保存可审计的 News 与 Trading 事实，并保留派生决策的来源和权限。本文件只统一容易混淆的术语，模块行为以当前源码与对应手册为准。

| 术语 | 当前语义 | 不应称为 |
| --- | --- | --- |
| **Item revision** | 同一来源记录在本地观察到的一次正文修订 | 仅由正文 hash 代表的唯一历史版本 |
| **EventUpdate** | 已采用的不可变知识版本，包含命题、引用、变化和问题 | 一次模型请求或一张已发送卡片 |
| **Notification intent** | 面向读者与精确内容的稳定发送意图 | 已发送事实 |
| **实际发送回执** | 适配器可证明的结果及精确正文；允许结果未知 | 计划选中命题的集合 |
| **Proposal** | 尚未接受的复核 / 研究建议，不拥有真值权威 | 临时真值、Draft Gold |
| **Gold / Accepted review** | 显式接受进入持久账本的复核参考 | 天然人工标签、普遍现实真理 |
| **Teacher Proposal** | 更审慎模型给出的候选建议，接受前仍是 Proposal | 自动 Gold、Teacher truth |
| **AI Adjudicator** | 明确身份的非人类评审者，结论保留该身份 | 人工审核员、模拟 operator |
| **Review UI projection** | 可替换的证据和复核展示 | 第二个接受复核数据库 |
| **冻结 Case** | 在明确知识截止、来源和市场证据上形成的研究实例 | 会随着最新行情悄悄改写的历史 |
| **Signal / OperatorIntent** | 具备明确作用域的建议发布 / 操作意图 | 交易所已受理或已成交 |

**Stable Prediction** 只在历史 Stable cohort 资料中指当时已经记录、评估时不重跑的生产预测。当前 EventUpdate 链路不要求 Stable / teacher / release 激活，也不能由此术语推断旧优化发布系统仍在线。

原始事实、模型理解、接受复核、读者实际收到的内容和交易所真实执行结果各有证据来源。某一层成功不能替另一层提供证明；未知值不以零、默认成功或“没有发生”代替。
