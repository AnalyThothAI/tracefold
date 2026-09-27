# Review：证据复核与卡片评审器校准

[手册](../README.md) · [News](news.md) · [术语](../../CONTEXT.md) · [测试](../TESTING.md)

当前保留两项明确能力：**ReviewDesk 的版本化复核**和**卡片评审器的固定语料校准**。它们不是旧版多预测器 News Program 的训练、GEPA 优化、发布与 canary 系统。

## 1. 代码与职责

| 所有者 | 职责 |
| --- | --- |
| [review/desk.py](../../tracefold/news/review/desk.py) | 复核队列、任务版本、证据视图、追加接受的判断与外部漏报 |
| [learning/judge.py](../../tracefold/news/learning/judge.py) | 保留的卡片评审器契约和调用 |
| [learning/judge_calibration.py](../../tracefold/news/learning/judge_calibration.py) | 固定扰动语料、评估与独立结果凭据 |
| [app/learning_runtime.py](../../tracefold/app/learning_runtime.py) | 当前评估执行的模型装配，不是线上自动发布器 |
| [CLI parsers](../../tracefold/app/cli/parsers/news.py)、[CLI commands](../../tracefold/app/cli/commands/) | 显式查询 / 写入命令与真实 reviewer 身份 |

## 2. 哪些东西可以叫 Gold

| 术语 | 正确含义 |
| --- | --- |
| Proposal | 尚未接受的建议，没有真值权威 |
| Accepted review / Gold | 明确接受并写入持久账本的复核参考；不保证独立人类准确性 |
| Teacher Proposal | 由更审慎模型产生的候选建议，接受之前仍是 Proposal |
| AI Adjudicator | 有明确非人类身份的评审者；不得写成人工复核 |
| Review UI projection | 可重建的展示，不是另一个接受记录数据库 |

“系统接受了标签”与“现实世界已验证该命题”不同。模型评分和训练集标签不能自动写入接受账本，Reviewer 身份也不能因为由人发起调用就改成人类。

## 3. ReviewDesk 的闭环

```mermaid
flowchart LR
    Task["任务与精确版本"] --> Evidence["固定证据视图"]
    Evidence --> Proposal["实际评审者的判断"]
    Proposal --> Accept["显式提交与接受"]
    Accept --> Ledger[("追加式复核账本")]
    Ledger --> Coverage["可审计覆盖与结果读取"]
```

任务引用必须绑定精确版本，可能指向 EventUpdate、意图或其证据。新版本出现不允许覆盖旧复核。幂等键用于同一次提交重试，不是把多个不同判断压成同一条事实。

```bash
# 以下查询使用具备数据库连通性的 Workers 容器
docker compose exec -T workers tracefold news review queue --hours 24 --limit 30
docker compose exec -T workers tracefold news review evidence TASK --version VERSION --source-only
```

`--source-only` 只提供绑定的来源证据，不展示待评 Agent 答案或其他复核，避免把看过原答案后的评分误称盲评。

`news review submit` 要求任务、版本、判断文件和真实 reviewer；它会写入接受记录。`external-miss` 追加系统外发现的遗漏及其 rubric。两者是显式数据操作，不因修改手册而运行。准确参数见[CLI 参考](../generated/cli-help.md)。

## 4. 评审器校准做什么

固定校准语料通过有意改变一处内容，测试评审器能否识别对应错误。实际命令需要明确模型，可能产生费用；输出是本次模型、语料与判断对应的结果凭据。

```bash
# 只查看参数，不请求模型
uv run tracefold news learning judge-calibration --help
```

真正运行时提供 `--model MODEL`，可用 `--out FILE` 保存 JSON。该命令不写生产数据库，不修改 accepted Gold，不激活运行时模型，不提供“优化后自动发布”的隐式通路。

```mermaid
flowchart LR
    Corpus["固定扰动语料"] --> Run["显式选择模型并校准"]
    Run --> Receipt["独立结果凭据"]
    Receipt --> Compare["核对错误类型与误判"]
    Compare --> Human["据证据决定下一步改动"]
```

固定合成语料适合检验已知错误类型，但不代表真实新闻分布、独立人类一致性或交易收益。需要业务评估时明确采样范围、事件版本、reviewer 与误差定义，不能从一个总分推出在线质量全面提升。

## 5. 旧研究与当前运行的边界

历史 verdict 词表和部分复盘聚合仍用于读取旧记录，不承担新 EventUpdate 的判断权。旧 Stable / teacher / GEPA / release 资料在历史研究语境中阅读，不能写成今天启动项目必须运行的步骤。

[notebooks](../../notebooks/README.md)保留离线工作说明；数据库 schema 和已接受标签属于审计历史，不因简化当前入口被篡改。

验证入口：[News judge 边界](../../tests/architecture/test_news_judge_boundary.py)、[news 测试目录](../../tests/news/)、[ReviewDesk 集成](../../tests/integration/test_news_review_desk.py)。文档或纯测试不应请求真实模型、提交评审或变更线上模型选择。
