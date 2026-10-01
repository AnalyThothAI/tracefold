# Review：卡片评审器校准

[手册](../README.md) · [News](news.md) · [术语](../../CONTEXT.md) · [测试](../TESTING.md)

当前保留固定扰动语料的卡片评审器校准。P2 已移除没有生产消费者的 ReviewDesk、通知反馈与外部漏报表，CLI 不再提供 `news review`，事件详情也不再包含 `feedback`。校准结果是独立评估凭据，不会自动训练、发布或激活生产模型。

<a id="section-哪些东西可以叫-gold"></a>
<a id="section-reviewdesk-的闭环"></a>
## 复核与真值

模型评分、接受标签和独立人类真值是不同证据。离线业务评估仍应记录采样范围、事件版本、实际评审者与误差定义；由人发起的 AI 评分不能写成人工复核。

<a id="section-评审器校准做什么"></a>
## 03 · 评审器校准做什么

固定校准语料通过有意改变一处内容，测试评审器能否识别对应错误。实际命令需要明确模型，可能产生费用；输出是本次模型、语料与判断对应的结果凭据。

```bash
# 只查看参数，不请求模型
uv run tracefold news learning judge-calibration --help
```

真正运行时提供 `--model MODEL`，可用 `--out FILE` 保存 JSON。该命令不写生产数据库，不修改 accepted Gold，不激活运行时模型，不提供“优化后自动发布”的隐式通路。

```mermaid
---
config:
  fontFamily: "system-ui, Noto Sans CJK SC, Microsoft YaHei, WenQuanYi Zen Hei, sans-serif"
  flowchart:
    curve: linear
    nodeSpacing: 28
    rankSpacing: 42
---
flowchart LR
    accTitle: 固定语料校准
    accDescr: 固定扰动语料经显式选择模型运行，产生独立结果凭据，供核对错误类型和决定后续改动。
    Corpus["固定扰动语料"] --> Run["显式模型校准"]
    Run --> Receipt["独立结果凭据"]
    Receipt --> Compare["核对错误类型与误判"]
    Compare --> Human["决定后续改动"]

    classDef news fill:#ecfdf5,stroke:#0f766e,color:#134e4a,stroke-width:1.5px
    classDef research fill:#eef2ff,stroke:#6366f1,color:#312e81,stroke-width:1.5px
    classDef execution fill:#fff7ed,stroke:#c2410c,color:#7c2d12,stroke-width:1.5px
    classDef store fill:#f1f5f9,stroke:#64748b,color:#1e293b,stroke-width:1.5px
    classDef external fill:#f8fafc,stroke:#94a3b8,color:#334155,stroke-dasharray:4 3
class Corpus,Run,Compare,Human news;
class Receipt store;
```

*评估视图 · 没有自动生产发布箭头；校准结果不会激活模型或改写接受记录。*

固定合成语料适合检验已知错误类型，但不代表真实新闻分布、独立人类一致性或交易收益。需要业务评估时明确采样范围、事件版本、reviewer 与误差定义，不能从一个总分推出在线质量全面提升。

<a id="section-旧研究与当前运行的边界"></a>
## 旧研究与当前运行的边界

旧 Stable、teacher、GEPA 和 release 资料属于历史研究。[notebooks](../../notebooks/README.md)保留离线工作说明。

<a id="section-源码责任地图"></a>
## 源码责任地图

| 所有者 | 职责 |
| --- | --- |
| [learning/judge.py](../../tracefold/news/learning/judge.py) | 卡片评审器契约和调用 |
| [learning/judge_calibration.py](../../tracefold/news/learning/judge_calibration.py) | 固定扰动语料、评估与结果凭据 |
| [app/learning_runtime.py](../../tracefold/app/learning_runtime.py) | 显式评估执行的模型装配 |
| [CLI](../generated/cli-help.md) | `news learning judge-calibration` 参数 |

验证入口：[News judge 边界](../../tests/architecture/test_news_judge_boundary.py)、[news 测试](../../tests/news/)。

<a id="section-常见误解"></a>
## 常见误解

校准语料只检验已知错误类型，不代表真实新闻分布、人类一致性或交易收益。总分不能证明在线质量全面提升。

[返回文档中心](../README.md) · [架构图谱](../ARCHITECTURE.md#atlas)
