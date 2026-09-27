# #725 编辑判断有限对照（2026-09-27）

固定输入为 [6 个合成 Event、7 条 Claim](../../tests/fixtures/news/issue_725_attention_cases.json)，由
[构造脚本](../../scripts/build_news_attention_cases.py)经真实 EventUpdate 合同生成。其中 3 条标为应推、
3 条为明确杂讯；另有 1 条拟议项目进展标为 `uncertain`，它不计入应推或应留 Feed 的错误数。
这些标签是工程评估样本，不是实际新闻人工 Gold，也不包含后见价格。

使用 [离线对照入口](../../scripts/eval_news_attention.py)先重放合成的“全部普通推”基线，再显式选择
当前 App 的卡片生成 route 主模型 `openai/qwen3.8-27b`，调用与生产相同的 `DspyAttentionAssessor`
和[编辑简报](../../tracefold/news/updates/editorial_brief.txt)。基线是合成反例，**不是旧线上政策的测量结果**。

| 指标 | 合成全部推基线 | 实际模型 |
| --- | ---: | ---: |
| 应推被留 Feed | 0 | 0 |
| 明确杂讯被通知 | 3 | 0 |
| `notify` / `key` / `feed_only` | 7 / 0 / 0 | 3 / 1 / 3 |
| 技术失败 | 0 | 0 |
| 物理模型调用 | 无网络 | 6 |
| 模型调用墙钟合计 | 无网络 | 19,689 ms |
| 输入 / 输出 token | 无网络 | 6,813 / 571 |
| 费用 | 无 | 不可得 |

[逐例原始报告](issue-725-attention-live-2026-09-27.json)保留每条选择、短理由、调用数和用时。
物理调用数与 token 由实际 DSPy LM history 读取；若供应商没有返回可核验历史，脚本会输出 `null`，
不会把零当作无调用。每例有 20 秒局部上限，失败按类型记录，不补造模型选择。

复现命令：

```bash
uv run python scripts/build_news_attention_cases.py
uv run python scripts/eval_news_attention.py --input tests/fixtures/news/issue_725_attention_cases.json
uv run python scripts/eval_news_attention.py --input tests/fixtures/news/issue_725_attention_cases.json --live-model openai/qwen3.8-27b
```

这个小样本只检查真实 route 是否按本次简报区分普通有用进展、诉讼事实与宣传，不估计线上漏报率、
整体精度、排队时延、EventUpdate 采用到发送时延或净成本。模型样本运行不创建发送意图、数据库连接或通知。
实际队列和发送延迟需要切换后的运行指标；本报告不以推送量下降作为验收。
