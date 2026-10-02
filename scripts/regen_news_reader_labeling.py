"""Synchronize the human labeling guide from the same owner rules as blind annotation."""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.news_reader_labeling import GUIDE_VERSION, OWNER_GUIDE

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs/modules/news-reader-labeling.md"


def document() -> str:
    return f"""# News 读者标注规范 v1

[News](news.md) · [方案 A 实现与认证状态](../reports/news-805-implementation.md)

本页由 `scripts/regen_news_reader_labeling.py` 从独立 owner 规则生成。规则来源是
`scripts/news_reader_labeling.py`，类型定义复用生产模型选项，资格布尔值复用 `PUSHABLE_KINDS`。
修改规则后运行 `python -m scripts.regen_news_reader_labeling --write`；`--check` 检查漂移。

当前每条标签必须写入的 `guide_version`：`{GUIDE_VERSION}`。
类型资格变化也改变该版本；旧 owner 与 Claude 标签不能改版本后继续冒充新真值。
问题不变时可以重用原始模型分布，但需要按新规范复核标签、重新拟合与认证。

## 标注对象与独立判断

对象是一个冻结的 `ReaderInput`：当时的命题、来源引文、首次可见日期 `as_of` 和实际已推消息。
先读全部来源和消息，再判断命题增加了什么。消息顺序可以随机化，但锚点 ID 必须映射回原顺序。
标注者看不到模型答案、旧推送决定、资格质量、概率或切线；抽样元数据保留在文件中，不进入盲标提示。
抽样单位和 `story_id` 也不能替代证据：同一故事的相关命题、跨语言改写和多个 Event 应归到同一个故事。

| 字段 | 填写标准 |
| --- | --- |
| `kind` | 使用下方同源的 10 类定义，先描述报道内容，再应用资格规则 |
| `push` | `push`：这位交易员希望收到新增信息；`feed`：不通知；`borderline`：仍需 owner 判断 |
| `anchor` | `none` 或提供的消息 ID；同主题不等于同事实，新数额、期限仍可保留锚点 |
| `key` | 是否应在几分钟内优先看到，且必须同时为 `push`；不得为了日量目标给重点 |
| `story_id` | 跨命题和 Event 一致的 actor/action/object 故事身份，用于隔离拟合与认证 |
| `note` | 解释新增信息、归类边界和推送/重点理由，不凭空补背景 |

## 已确认决定和边界例

资格表示“可以进入概率判断”，不意味着每条该类报道必须推送。小项目在交易范围内。项目自报的
TVL、存款或持有人里程碑按 `self_reported_metric` 归类，再应用下方同源资格规则。
单一来源、地点明确的具体事件保留来源归属，不能写成已证实事实。
日程数据由新增影响区分轻重，首次发布的就业、通胀、央行决议、GDP、主要公司交付与业绩具有明确影响，
不要求报道写明超预期；提醒、旧期次附注和重复数字仍按其内容归类。

重点确认例：非农远低于预期、霍尔木兹打通、G7 释放至多一亿桶库存、G7 搁置美国柴油出口禁令、
利雅得爆炸报道、特斯拉交付超预期、纳指历史新高、美国司法部没收 USDC、Blast 宣布关闭。
它们解释 owner 的时间优先标准，不是关键词名单。其他报道仍依其具体新增信息独立判断。

利雅得单一来源爆炸报道不能因为“未证实”自动当背景；相同事件的新日期、发生阶段或不同归属主张可能是新事实。
既有动作新增重要库存量、收件方或二十天期限，不因已锚定就自动 feed。
新季度首次发布与旧季度数字被附注引用不同；九月回顾、周报、钱包招揽和分析师目标价按实际类别判断。
命题来源冲突时保留归属和不确定性，不将模型或另一模型的一致意见当成事实验证。

## 同源的完整英文规则与类型定义

下方内容也是 Claude 盲标使用的规则。资格规则变化时，本段和版本必须同时变化；类型定义不随资格布尔值改写。

```text
{OWNER_GUIDE}
```

## 标签记录与复核

owner 金标使用 `labeler=owner`，Claude 标签使用其实际 annotation identity，两个原始日志分别保存。
每行携带 `case_id`、`reader_input_sha256`、上述 `guide_version`、`story_id` 和 `label`。
认证用 owner 标签另需最终联合入样概率、抽样设计、抽样单位和冻结认证抽样框；原始 claim 导出的概率
不自动等于后续 owner 选择或故事代表选择的联合概率。

`owner-sample` 输出保留原始输入用于核对。人工标注前运行 `label_news_reader prepare-owner`，
只交付它生成的 public 文本文件：命题、引文、来源、日期、随机排序消息和 `blind_input_sha256`。
它去掉抽取字段、旧决定、代理故事和抽样元数据；原摘要、抽样信息及消息 ID 映射保存于私有 manifest。
人工返回每行 `case_id`、所见 `blind_input_sha256`、`story_id` 和 `label`；
`import-owner` 核验完整抽中名单、原输入与盲材料摘要，恢复原 anchor ID 并补齐 owner/规范/最终联合概率。
不要直接把原 `reader_input` 文件或私有 manifest 交给盲标者。两个命令只读写本地文件，不调用模型。

Claude 标签只能参与拟合和复核，不计入切线以上 owner 标签的 150/60 计数。不将定向难例或按钮反馈作为概率样本。
`borderline` 不进入二元拟合，在预先声明的认证处理中作为非 push；不得看到分数后改成正例。
逐字段报告混淆矩阵、κ/二次加权 κ 与重点分歧；折外复核队列只使用拟合集，不能自动改标签或读取认证标签调问卷。

## 抽样、预算与认证边界

先固定抽样框、单位、分层、入样概率和较早/最近时间边界，再收 owner 标签。
原始导出按决策带 × 已存类型分层；历史 v3 缺类型时明确为 unknown，不用关键词伪造分类。
认证需要独立故事代表的已知概率抽样证明；缺失时保持未认证，不能把 claim 框改名为故事框。
`owner-sample` 只从完整 claim census（每行原始入样概率为 1、框总数与文件行数一致）建立最近故事框。
它先重建冻结候选，再固定每故事最早的一条代表、分层总量和抽中 ID，输出盲标请求与私有 manifest。
认证总体只覆盖这些故事代表，不扩大为全部生产命题或 Event/卡片。非 census 原始分层样本仍可拟合，
但当前工具不能由其证明故事总体；需要另行有证据的联合概率设计。
完整故事归组必须在 owner push/key 标签到来前独立复核；`--story-grouping-reviewed` 记录该项审阅，
不是让代理分组自动成为独立故事。若 owner 后来合并了抽中故事，工具拒绝以其作为独立计数。

拟合绑定较早标签、全部冻结输入/模型回答/拆分和拟合配置；认证重新计算 m*、系数、折外预测及切线序列，
即使修改参数后重算候选自身摘要也不能通过。最近部分允许按冻结选择加入 owner 金标；必须保留全部代理日志。
认证 CLI 要求持久 `--holdout-ledger`，拒绝同一后端 holdout 换候选或更换已经读取的金标数据；相同产物可重放。
该本地记录不能证明没人从别处看过标签，所以激活还需 owner 审阅 holdout 使用记录。
题目、适配器或模型改变需真实重问；看过结果不得调参后复用认证集。

约 200 条 owner 标签是预算起点。如果均匀跨 70/30 时间切分，最近部分约 60 条，不能达到切线之上
推送至少 150 条的要求。应在标注前为认证和重点候选分配样本、记录联合概率，或扩充预算；Claude 不能补认证计数。
日量按实际 Event/卡片回放验证，命题加权日量不是卡片量。模型失败保留在完整流程召回、覆盖率和耗时分母。
故事代表框保留来源最近完整日历中的零推送日期，边界首日不足一天时明确标记，不把它冒充完整生产日量。
精度切线认证通过也不代替类型、配对召回、双峰、真实延迟、卡片日量、owner 取舍和部署审阅。
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--write", action="store_true")
    commands.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = document()
    if args.write:
        DOCUMENT.write_text(expected, encoding="utf-8")
    elif not DOCUMENT.exists() or DOCUMENT.read_text("utf-8") != expected:
        parser.exit(1, "news_reader_labeling_document_drift\n")


if __name__ == "__main__":
    main()
