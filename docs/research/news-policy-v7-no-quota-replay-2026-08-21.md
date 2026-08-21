# Policy v7 无读者额度：最近 24 小时反事实回放

日期：2026-08-21（Asia/Taipei）
窗口：`[1787221881358, 1787308281358)`，即固定 24 小时半开区间
数据：生产 PostgreSQL，只读；1604 个 live、已有 Triage verdict 的 Event

## 结论

删除每小时、2 小时和 4 小时数量额度，不等于把 80 张旧 `throttled` 全部放出来。

- 旧 policy：1110 drop、397 push、17 escalate、80 throttled。
- v7 顺序反事实：1110 drop、416 push、20 escalate、58 duplicate-throttled。
- 净变化：可发送从 414 增到 436，增加 22 张，即约 `+5.3%`，平均每小时约增加 `0.92` 张。
- 80 张旧 throttle 中，21 张来自 storyline 数量额度，2 张来自 hourly quota；这 23 张在 v7 不再因数量被拦。
- 顺序 replay 最终释放 29 张旧 throttle；同时，扩大的模拟 sent ledger 让 7 张旧 push 被内容相似性识别为同事实重复，所以净增是 22，不是 29。

这个结果是**流量影响预测**，不是新闻质量分数。它没有使用 1H/4H 涨跌，也不能证明新增 22 张都值得推。

## 回放怎么做

1. 按 `news_events.opened_at_ms` 取固定窗口内的 live Event。
2. 每个 Event 只用 latest Triage 的冻结 verdict；不重新调用模型。
3. 保留现有语义阈值、restatement、pause/mute 与 duplicate similarity，只删除数量额度。
4. stable/v7 按时间顺序各自维护“反事实读者已收到”账本；v7 判定为 push/escalate 时才加入自己的账本。
5. duplicate 只与之前四小时的模拟 sent headline 比较；方向反转、escalate 和 degraded fallback 不因相似文本被拦。
6. 假设符合条件的卡在 Event 时间成功发送；真实 webhook 失败、网络延迟和 operator pause 不在本次流量估计中。

核心伪代码：

```python
for event in chronological_events:
    sent_ledger.expire(before=event.opened_at - 4h)
    result = decide_v7(
        frozen_verdict=event.latest_triage,
        told=event.trace.told,
        seen=sent_ledger,
    )
    if result in {"push", "escalate"}:
        sent_ledger.append(counterfactual_receipt(event))
```

## 为什么净增不大，但结构更正确

旧 80 张 throttle 的来源：

| 旧原因 | N | v7 含义 |
|---|---:|---|
| 已判同事实重复 `:seen` | 57 | 重新按 v7 的顺序 sent ledger 判断；大部分仍拦 |
| storyline count/cap/hard | 21 | 数量不再有否决权 |
| hourly cap | 2 | 每小时数量不再有否决权 |

v7 的 58 张 throttle 全部来自内容相似性，不来自“今天已经推了多少”。因此系统仍能挡住 OKX 模板批次、同一官员同一表态的复述，但不会因为中东、宏观或某个资产新闻多，就把一个不同的新事实挡掉。

## 代表性变化

被旧数量额度挡住、v7 会发出的例子：

- `e9b54dc…`：墨西哥油企开发页岩原油，旧 key 被错误归入 `mideast_energy:hard18`；v7 不再让错误 storyline 消耗额度并否决事实。
- `74f8bd41…`：Trump 对伊朗贸易伙伴施压，旧 `hourly_cap`，v7 为 push。
- `30dd82d8…`：加密清算达到 10 月 10 日以来最高，旧 `hourly_cap`，v7 为 push。
- `25f166b…`：伊朗袭船造成的大规模漏油卫星证据，旧 `mideast_energy:hard18`，v7 为 high-priority push。

v7 新识别为同事实重复的旧 push 例子：

- `985ef2ca…`：Bessent 呼吁伊朗政权更迭，与此前 sent 表态相似。
- `a3aba9e9…`：Bessent 宣布对伊朗最严厉制裁，与此前 sent 表态相似。
- `61773b4d…`：Bitcoin/Clarity Act 进展与此前 sent 卡片高度相似。

这些只是待人工 ReviewDesk 复核的案例，不是自动 gold label。

## 上线后应观察什么

- 预期推送量短期约上升 5%，但内容供给变化会让真实值偏离；不能拿本次窗口当永久容量预测。
- `throttled_by` 新写入应只剩 `storyline:<key>:seen`；出现 `cap/hard/hourly_cap` 说明旧 worker 仍在运行。
- 发送量可以在独立运营报表中按任意时间粒度观察，但生产 Repository、Agent 输入与决定参数不再计算或携带旧额度字段。
- ReviewDesk 要优先抽样“v7 新增发送”与“duplicate withheld”，分别检查 recall gain 和 false duplicate。
- reader load 是 release report 的 guardrail 和人工产品指标，不再是热路径 quota。
