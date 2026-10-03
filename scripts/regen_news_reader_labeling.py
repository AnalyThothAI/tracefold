"""Synchronize the human labeling guide from the same owner rules as blind annotation."""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.news_reader_labeling import GUIDE_VERSION, OWNER_GUIDE

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs/modules/news-reader-labeling.md"


def document() -> str:
    return f"""# News 读者标注规范 v6

[News](news.md) · [方案 A 实现状态](../reports/news-805-implementation.md) ·
[推送认证](../reports/news-805-certification.md)

本页由 `scripts/regen_news_reader_labeling.py` 从独立 owner 规则生成。规则来源是
`scripts/news_reader_labeling.py`，类型定义复用生产模型选项，资格布尔值复用 `PUSHABLE_KINDS`。
修改规则后运行 `python -m scripts.regen_news_reader_labeling --write`；`--check` 检查漂移。

新标注写入的 `guide_version`：`{GUIDE_VERSION}`。
类型资格变化也改变该版本。每条标签记录它实际使用的规范版本；候选模型记录拟合标签的版本，
认证它的 owner 标签必须是同一版本，运行时校准文件也记录该版本。当前推送认证使用 v5 标签，
v6 用于新的标注轮次；旧标签不能改版本号后冒充新规范的真值。

## 分工：代理判重，owner 只答推送和重点

对象是一个冻结的 `ReaderInput`：当时的命题、来源引文、首次可见日期 `as_of`、实际已推消息，
以及输入之外的被引用原文 `source_text`。读者模型看不到 `source_text`，标注者用它补全命题。

- **Claude 代理**按下方完整规则给出 `kind`、`anchor`、`repeat`、`push`、`key`、`story_id` 和 `note`。
  `repeat=true` 表示锚定消息已写明该命题的全部实质信息，此时 `push` 必须为 `feed`。
- **owner** 只回答“如果这是新信息，推不推、是不是重点”：每行
  `{{case_id, push, key, dup, note, guide_version, reviewed_at}}`。代理判为重复的条目页面会提示，
  owner 用 `dup` 确认（`agree`）或推翻（`disagree`）；未提示的条目 `dup` 为 null。
- `import-owner` 合并两者：`kind` 和 `anchor` 取同一条的代理标签，`push`/`key` 取 owner 回答；
  owner 确认重复时最终为 `feed`、非重点，推翻时用 owner 的回答。每个字段的来源写在 `label_sources`。

## 按原文补全后的事实标注

命题是 `source_text` 中一个命题的英文改写。命题省略主体、对象或地点而原文写明时（例如原文标题是
G7 库存释放，命题只写“释放将在 4 个月内完成”），按补全后的事实标注；推送卡片会写出主体。
只有原文也看不出说的是什么时，才按背景、不推处理。补全只补主体和对象，不能用原文里的另一条事实替换命题本身。

## 按预算定尺度

推送是选择性的：典型的一天，进入这一步的命题约每 4 条有 1 条值得推，约每 6 条推送有 1 条重点
（约每 40 条命题 1 条）。用这个稀缺程度决定一条命题需要多少具体、可交易的新信息；
仍然逐条独立判断，不在一批里配额或计数。

## 已确认决定和边界例

- **小项目**：任何规模的加密项目，具体的新动作或数字（上线、上币、集成、合作、治理结果、回购、抛售、
  抵押或金库决定、项目自报的存款/TVL/用户/持有人里程碑）推；预告、空泛计划、感谢、奖励机制和推广不推。
- **官员表态**：政府首脑、央行决策者及财政、贸易、能源、外交、国防官员关于货币、汇率、贸易、制裁、
  国家间军事、能源、航运或财政政策的新表态，执行前就推。
- **政策预期**：市场对央行政策预期的任何变化（利率概率、期货或掉期定价、数据或讲话后的交易员预期）推；
  每个新读数都是新信息，只有相同读数才算重复。
- **市场反应**：有明确原因且幅度明显的指数、收益率、汇率、商品或主要资产波动推。知名个股或主要代币单日
  约 8% 以上的波动即使没写原因也按 `market_move` 推，但没有催化剂不算重点；小幅例行波动和孤立报价是背景。
- **日程数据**：就业、通胀、央行、GDP、主要公司交付和业绩的一级发布即使没写超预期也推；
  次要发布需要有实质新影响。
- **历史背景**：用来解释当前事件的过去融资、事件经过、项目历史不推。附在公司新闻后、关于该公司的最新
  已公布实际值与机构预期推，单独不算重点；纯周报、月报、回顾是 `recap_or_old_period`，不推。
- **超预期与小偏差**：已发过的数字后来被报道明显超出或低于预期，是新信息，可以是重点（如交付超预期、
  非农远低于预期）；与预期只有小偏差（如失业率 4.2% 对预期 4.1%，且 4.2% 已发过）算重复。
- **重复**：已推消息以相同数字、条件和阶段写过核心事实时，锚定该消息并标 `feed`，无论故事多重要；
  新的规模、期限、对象、政策要求、归属或行动阶段可以在保留锚点的同时推。
- **不推**：推广和招揽；分析师目标价和无上述官方身份者的观点；背景、解释性内容和日历提醒。

`recap_or_old_period` 的资格为可推：附在当期公司报道后的最新财报行可推，纯回顾靠低影响留在信息流。
单一来源、地点明确的具体事件保留来源归属，仍可推送，不写成已证实事实。

重点确认例：非农远低于预期、加息预期明显转向、主要公司交付或业绩超出或低于预期、主要石油航道重开、
协同战略库存释放、出口禁令推迟、主要首都有具体归属的爆炸报道、主要指数历史新高、大额代币没收、项目宣布关闭。
它们解释 owner 的时间优先标准，不是关键词名单。

## 交易范围（D12–D14）

规范中的 Scope 段写明：主要交易美股、加密资产、黄金和原油；农产品（大豆、糖、乙醇、棕榈油、油菜籽、玉米、
小麦等）及其 USDA 或行业报告和价格不推；美国利率、债券、数据和政策都在范围内，中国、欧盟、日本、韩国只看央行
决策和重大政策，其他国家的宏观和政策不推；地缘冲突只推涉及美国或中东能源（伊朗、霍尔木兹、沙特、以色列、
美军行动）的，其他冲突不推，涉及美国制裁或停火谈判的除外。

这些范围规则目前只约束标注，不在运行时执行：读者四题和资格表都不表达交易范围，改动读者问题会使当前推送
认证失效。下一轮把范围写进读者问题，重问、重新拟合并重新认证。

## 同源的完整英文规则与类型定义

下方内容也是 Claude 盲标使用的规则。资格规则变化时，本段和版本必须同时变化；类型定义不随资格布尔值改写。

```text
{OWNER_GUIDE}
```

## 标签记录与复核

代理标签使用其实际 annotation identity 作为 `labeler`，owner 标签使用 `labeler=owner`，两份原始日志分别保存。
每行携带 `case_id`、`reader_input_sha256`、`guide_version`、`story_id` 和 `label`；代理行还带 `repeat`。

人工标注前运行 `label_news_reader prepare-owner`，只交付它生成的公开材料：命题、引文、来源、原文、日期、
随机排序的消息和 `blind_input_sha256`。抽取字段、旧决定、模型分数、代理建议和抽样信息都不进入公开材料；
原摘要、选择身份及消息 ID 映射保存于私有 manifest。`import-owner --labels … --proxy …` 核验完整抽中名单、
原输入与盲材料摘要和规范版本，按上面的分工合成 owner 标签。两个命令只读写本地文件，不调用模型。

Claude 标签只能参与拟合和复核，不计入 owner 认证标签。`borderline` 不进入二元拟合，认证时按非推送处理。
`label_news_reader report` 报告 owner 与代理在推送/重点上的混淆矩阵、κ 和判重确认数；
折外复核队列只使用拟合集，不能自动改标签。

## 抽样与认证边界

认证总体是候选时间边界之后完整 claim census 中的独立故事代表：故事是命题所属 Event 与已记录命题链接在整个
census 上的并集，有成员早于边界的故事整体排除，代表取 `(first_available_at_ms, case_id)` 最早的成员。
`owner-sample` 用冻结候选给每个代表打分，在看标签前按分数分层：推送区 R（资格通过且 `p_push ≥ c_L`）、
重点区 K（资格通过、`p_key ≥ k_L` 且不在 R）和其余 B；每层给定样本量或全查，并记录种子、总体、抽中名单和入样概率。
它也能校验别处在标签前冻结的选择：重新计算故事框和全部分数，逐条核对。

认证时每条切线选中的故事集合由分数精确已知，只对其中 owner 推送比例取界：各层 Clopper–Pearson 下界按层内
选中量加权，误差在有选中量的层间 Bonferroni 分配，没有标签的层计 0。切线从第一个选中量达到最小数的切线开始，
由严到松检验到 `c_L`，首次失败即停；通过还需至少 150（推送）/ 60（重点）个 owner 标注的独立故事。
推送与重点各用单侧 δ=0.05。重点只在推送通过后检验，误差再按推送可能选中的切线数分配。

精度证书只回答选中故事中 owner 认为该推的比例，不代替类型、召回、延迟、卡片日量和 owner 取舍；
这些门槛要么测得通过，要么由 owner 在发布审阅中逐项书面豁免。
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
