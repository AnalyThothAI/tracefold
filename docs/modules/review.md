# Review：证据复核与卡片评审器校准

[手册](../README.md) · [News](news.md) · [术语](../../CONTEXT.md) · [测试](../TESTING.md)

当前保留两项明确能力：**ReviewDesk 的版本化复核**和**卡片评审器的固定语料校准**。它们不是旧版多预测器 News Program 的训练、GEPA 优化、发布与 canary 系统。

| 模块速览 | 说明 |
| :--- | :--- |
| **定位** | 信息产品 / 复核与评估 |
| **运行位置** | 显式 CLI · ReviewDesk 与卡片评审器校准 |
| **输入 → 产物** | 绑定版本的任务与证据、固定扰动语料 → 追加式接受记录、独立校准结果凭据 |

> [!IMPORTANT]
> 接受的复核不等于独立人类真值；校准不会自动训练、发布或激活生产模型。

[复核术语](../../CONTEXT.md) · [测试边界](../TESTING.md)

<details>
<summary><strong>本页目录</strong></summary>

1. [哪些东西可以叫 Gold](#section-哪些东西可以叫-gold)
2. [ReviewDesk 的闭环](#section-reviewdesk-的闭环)
3. [评审器校准做什么](#section-评审器校准做什么)
4. [旧研究与当前运行的边界](#section-旧研究与当前运行的边界)
5. [源码责任地图](#section-源码责任地图)
6. [常见误解](#section-常见误解)

</details>

<a id="section-哪些东西可以叫-gold"></a>
## 01 · 哪些东西可以叫 Gold

| 术语 | 正确含义 |
| --- | --- |
| Proposal | 尚未接受的建议，没有真值权威 |
| Accepted review / Gold | 明确接受并写入持久账本的复核参考；不保证独立人类准确性 |
| Teacher Proposal | 由更审慎模型产生的候选建议，接受之前仍是 Proposal |
| AI Adjudicator | 有明确非人类身份的评审者；不得写成人工复核 |
| Review UI projection | 可重建的展示，不是另一个接受记录数据库 |

“系统接受了标签”与“现实世界已验证该命题”不同。模型评分和训练集标签不能自动写入接受账本，Reviewer 身份也不能因为由人发起调用就改成人类。

<a id="section-reviewdesk-的闭环"></a>
## 02 · ReviewDesk 的闭环

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
    accTitle: 版本化 ReviewDesk
    accDescr: 绑定版本的任务提供固定证据；实际评审者显式提交判断，接受记录追加到账本，再用于覆盖与结果读取。
    Task["版本化任务"] --> Evidence["固定证据视图"]
    Evidence --> Proposal["具名评审判断"]
    Proposal --> Accept["显式提交与接受"]
    Accept --> Ledger[("追加复核账本")]
    Ledger --> Coverage["结果与覆盖读取"]

    classDef news fill:#ecfdf5,stroke:#0f766e,color:#134e4a,stroke-width:1.5px
    classDef research fill:#eef2ff,stroke:#6366f1,color:#312e81,stroke-width:1.5px
    classDef execution fill:#fff7ed,stroke:#c2410c,color:#7c2d12,stroke-width:1.5px
    classDef store fill:#f1f5f9,stroke:#64748b,color:#1e293b,stroke-width:1.5px
    classDef external fill:#f8fafc,stroke:#94a3b8,color:#334155,stroke-dasharray:4 3
class Task,Evidence,Proposal,Accept,Coverage news;
class Ledger store;
```

*复核视图 · 接受是账本状态，不是独立人类准确性的保证。*

任务引用必须绑定精确版本，可能指向 EventUpdate、意图或其证据。新版本出现不允许覆盖旧复核。幂等键用于同一次提交重试，不是把多个不同判断压成同一条事实。

```bash
# 以下查询使用具备数据库连通性的 Workers 容器
docker compose exec -T workers tracefold news review queue --hours 24 --limit 30
docker compose exec -T workers tracefold news review evidence TASK --version VERSION --source-only
```

`--source-only` 只提供绑定的来源证据，不展示待评 Agent 答案或其他复核，避免把看过原答案后的评分误称盲评。

`news review submit` 要求任务、版本、判断文件和真实 reviewer；它会写入接受记录。`external-miss` 追加系统外发现的遗漏及其 rubric。两者是显式数据操作，不因修改手册而运行。准确参数见[CLI 参考](../generated/cli-help.md)。

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
## 04 · 旧研究与当前运行的边界

历史 verdict 词表和部分复盘聚合仍用于读取旧记录，不承担新 EventUpdate 的判断权。旧 Stable / teacher / GEPA / release 资料在历史研究语境中阅读，不能写成今天启动项目必须运行的步骤。

[notebooks](../../notebooks/README.md)保留离线工作说明；数据库 schema 和已接受标签属于审计历史，不因简化当前入口被篡改。

验证入口：[News judge 边界](../../tests/architecture/test_news_judge_boundary.py)、[news 测试目录](../../tests/news/)、[ReviewDesk 集成](../../tests/integration/test_news_review_desk.py)。文档或纯测试不应请求真实模型、提交评审或变更线上模型选择。

<a id="section-源码责任地图"></a>
## 05 · 源码责任地图

| 所有者 | 职责 |
| --- | --- |
| [review/desk.py](../../tracefold/news/review/desk.py) | 复核队列、任务版本、证据视图、追加接受的判断与外部漏报 |
| [learning/judge.py](../../tracefold/news/learning/judge.py) | 保留的卡片评审器契约和调用 |
| [learning/judge_calibration.py](../../tracefold/news/learning/judge_calibration.py) | 固定扰动语料、评估与独立结果凭据 |
| [app/learning_runtime.py](../../tracefold/app/learning_runtime.py) | 当前评估执行的模型装配，不是线上自动发布器 |
| [CLI parsers](../../tracefold/app/cli/parsers/news.py)、[CLI commands](../../tracefold/app/cli/commands/) | 显式查询 / 写入命令与真实 reviewer 身份 |

<a id="section-常见误解"></a>
## 06 · 常见误解

<details>
<summary><strong>展开常见问题</strong></summary>

**Gold 就是独立人工真值吗？**

不一定。它表示已显式接受的复核记录，实际 reviewer 与证据版本仍要保留。

**校准通过会自动切换线上模型吗？**

不会。校准输出独立结果凭据，没有自动训练或发布权限。

</details>

---

[返回文档中心](../README.md) · [架构图谱](../ARCHITECTURE.md#atlas) · [返回顶部](#review证据复核与卡片评审器校准)
