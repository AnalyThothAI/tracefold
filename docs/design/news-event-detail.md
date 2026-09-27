# News 详情页：阅读与交互设计记录

[文档中心](../README.md) · [前端架构](../FRONTEND.md) · [News 模块](../modules/news.md)

**先读当前内容，再解释通知，最后展开历史与处理记录。**

> [!NOTE]
> 这是 #722 对应的设计与预览记录，不是实时监控页面。原型使用基于一个 Event 的示例内容；截图保留当时的视图与数值，不代表当前运行结果。

## 背景与阅读目标

这次调整承接 [#720](https://github.com/AnalyThothAI/tracefold/issues/720) 的命题数量与重复变化解释，相关记录见 [#722](https://github.com/AnalyThothAI/tracefold/issues/722) 和 [PR #721](https://github.com/AnalyThothAI/tracefold/pull/721)。[交互原型](news-event-detail-prototype.html)先于 React 页面修改准备；最终应用仍使用已有 shell 和设计 token。

| 阅读顺序 | 页面需要回答 |
| :--- | :--- |
| **01 当前内容** | 发生了什么，当前共有多少条命题？ |
| **02 通知判断** | 为什么通知或没有通知，原因对应哪条命题？ |
| **03 来源证据** | 每个说法由哪份材料支持，有哪些关系或分歧？ |
| **04 历史与观察** | 历史比较、处理记录和市场观察如何查看？ |

## 交互决定

每条当前命题只出现为一个编号阅读单元，带首个引用；历史比较、结构化字段与更多引文在该命题内部展开。变化标签描述关系，不能被当成新增的一条当前命题。

通知理由在桌面上紧邻内容，在窄屏上放在内容之后，并回链到相应命题。页面目录连接来源、处理记录与市场数据，完整原文与分歧仍然可查看。

当前滚动报价与 Event 锚定的价格反应分开呈现，避免将两种时间口径混为一个收益指标。

## 原型与实际预览

先看宽屏阅读节奏，再检查窄屏的信息顺序。截图是已有审阅证据，不能用原型图冒充运行截图。

<details>
<summary><strong>桌面 · 1440 px</strong></summary>

**交互原型**

![新闻详情桌面原型，示例内容用于检查阅读顺序](news-event-detail-prototype-desktop.png)

**React 预览**

![新闻详情 React 桌面历史预览，包含真实 API 标签与工作台外壳](news-event-detail-preview-desktop.png)

</details>

<details>
<summary><strong>窄屏 · 390 px</strong></summary>

**交互原型**

![新闻详情窄屏原型，检查内容与通知的上下顺序](news-event-detail-prototype-mobile.png)

**React 预览**

![新闻详情 React 窄屏历史预览](news-event-detail-preview-mobile.png)

</details>

原记录中的 React 截图由本地 Vite 应用读取已有 Serve API，Event ID 为 `cf4ba24aa1221d9e5c03580685d0328184b5349cc8495db7d74824833e34a70d`。完整工作台外壳与 API 实际标签会造成它与原型的差别；Event、报价与文案之后都可能变化。本轮文档整理没有重新访问该生产 Event 或测量页面性能。

---

继续阅读：[前端数据与状态](../FRONTEND.md) · [新闻通知语义](../modules/news.md#notification)
