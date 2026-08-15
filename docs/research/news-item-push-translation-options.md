# News Item Push 翻译方案调研

更新时间：2026-08-15

## 结论

Tracefold 的 Push 翻译不应继续把“通用聊天模型 + JSON 输出”当作长期默认实现。这个场景只有一个很窄的任务：把一条至多 500 字符的市场新闻标题翻成简体中文，并保留数字、百分比、金额、`$TOKEN` 和大写资产符号。专用机器翻译 API 的接口、延迟和失败语义都更贴合这个任务。

推荐的 KISS 目标形态是：

1. `NewsItemPush` 内部保留一个可选的单一 Translator seam；不新增翻译表、队列、Worker、状态机或健康门槛。
2. 使用一个固定的专用机器翻译供应商；不做运行时供应商选择、瀑布或自动 fallback。
3. 使用原生异步 HTTP 客户端和绝对 1.5 秒 deadline；单次请求、零重试，超时或校验失败立即发送原文。
4. 原文始终显示，现有中文输出和锚点校验继续保留。
5. 先从真实上海 Worker 对候选供应商做离线赛马。若 DeepL 在真实网络下通过门槛，首选 DeepL `latency_optimized`；否则固定使用阿里云机器翻译专业版 `finance`。百度高级版可作为赛马候选，但不是运行时备用供应商。

如果现在不愿增加一个独立翻译凭据，则先做“止血版”：仍用现有 DeepSeek 凭据，但改成异步、纯文本短输出、绝对 deadline；这能消除当前线程占用风险，却不是最终最合适的翻译产品。

## 当前实现与实测

当前实现位于：

- `src/tracefold/integrations/news_push.py`：同步 `httpx.Client` 调 OpenAI-compatible `/chat/completions`，要求 JSON 输出，HTTP timeout 2.25 秒。
- `src/tracefold/news/push.py`：经共享 `FiniteOperations` 线程池执行，operation budget 2.5 秒、外层 total budget 3 秒；失败后原文 fallback。
- `src/tracefold/app/worker_capabilities.py`：全进程共享三个同步外部操作线程；调用方超时不会杀死底层线程，permit 要等底层 future 真正完成才释放。
- `src/tracefold/app/workers.py` 与 `src/tracefold/platform/config/settings.py`：Push 翻译复用 `llm.api_key`、`llm.base_url` 和 `llm.news_brief_model`，因此翻译供应商和 Brief 模型配置被隐式绑在一起。

现有实现已经做对了几件重要的事：翻译不是 Push admission 或 health gate；中文标题跳过翻译；原文始终保留；输出有长度、中文字符和锚点校验；翻译发生在 Feishu send fence 之前；失败直接发送原文；没有翻译重试。

生产小样本（2026-08-15，样本仍很小，只用于发现方向而非建立 SLO）：

- 最近 24 小时有 1,859 条 live NewsItem；平均标题约 97.4 字符，P50 83、P95 234、最大 500。
- 按这个流量外推，月翻译输入约 543 万字符。
- 切换后最初 10 条有翻译尝试的 Push：6 条成功翻译、2 条超时 fallback、2 条因锚点变化 fallback。
- 6 条成功翻译平均约 1.36 秒，样本 P95 约 1.67 秒；2 条超时记录平均约 3.47 秒。
- 所有这些 Push 最终都能靠原文 fallback 继续发送；问题主要是翻译收益不稳定且增加发送延迟。

### 当前 DeepSeek 路径的隐藏风险

DeepSeek 官方说明：非流式请求在等待推理期间会持续返回空行以维持连接，而且可能等待很久。HTTP 客户端的 read timeout 通常按“多久没收到任何字节”计算；空行会重置 read timeout。因此 2.25 秒的 `httpx` timeout 不是可靠的总时限。外层 asyncio timeout 虽然会让 Push 回退原文，但不能中止正在同步线程里执行的请求；该线程与共享 permit 仍可能被占用。

这比单条翻译超时更重要：连续几个慢请求可能耗尽三个共享 `FiniteOperations` 线程，进而影响其他同步外部操作。DeepSeek 的 JSON Output 官方文档还明确提到偶尔可能返回空内容；对只需要一个字符串的标题翻译，JSON 生成和二次解析没有必要。

来源：[DeepSeek rate-limit/keepalive 说明](https://api-docs.deepseek.com/quick_start/rate_limit)、[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode)、[DeepSeek Chat Completion API](https://api-docs.deepseek.com/api/create-chat-completion)。

## 候选方案比较

| 方案 | 接口与场景匹配 | KISS/运维 | 中国大陆部署考虑 | 结论 |
|---|---|---|---|---|
| DeepL API | 直接 translate API；支持 `ZH-HANS`、context、glossary 和 `latency_optimized` | 单 API key、直接 REST；可禁用 SDK 自动重试 | 必须从真实上海 Worker 验证连通率和 P95 | API 形态最佳，通过实测则首选 |
| 阿里云机器翻译 | 通用版支持 context；专业版有 `finance` 场景 | RPC 签名/SDK 和两段凭据比 DeepL 重 | 杭州公网端点，本地网络路径最可控 | DeepL 不过门槛时的首选 |
| 百度通用翻译 | 简单 HTTPS form API；支持术语干预 | APPID + secret，MD5 签名较简单 | 大陆路径友好；高级版 QPS 10 足够串行 Push | 合理的离线赛马候选 |
| Google Cloud Translation Basic | 直接 v2 API + API key；标准 NMT | Basic 很简单；Advanced 的 IAM/区域/glossary 更重 | 必须实测中国大陆网络；不应为 glossary 引入 Advanced | 海外部署候选，不是上海默认 |
| Azure Translator | 直接 REST；支持 dynamic dictionary | Azure 资源、区域、key 配置中等 | 可选中国相关区域需结合账号与合规 | 无明显优势，不优先 |
| AWS Translate | 专用翻译、custom terminology | SigV4/SDK、IAM 和区域配置更重 | 网络与账号路径需验证 | 对当前单一窄场景不够 KISS |
| 腾讯云机器翻译 | 历史文档仍能找到文本翻译价格/术语资料 | — | `TextTranslate` 已于 2026-07-08 删除 | 排除，不应新接入 |
| 本地 OPUS/CTranslate2 | 无网络调用，可量化和单线程推理 | 模型打包、内存、冷启动、质量评估和升级都由本项目承担 | 网络最稳定，但抢占当前 2 GB/2 CPU Worker | 当前不值得 |
| 本地 NLLB-200 | 多语言覆盖好 | 600M 模型仍有显著资源和维护成本 | 同上 | 官方模型卡称非生产用途且 CC-BY-NC，排除 |
| 继续通用 LLM | prompt 可控制金融语气和锚点 | 生成协议、输出校验、长尾延迟更复杂 | 当前 DeepSeek 路径已出现超时 | 仅适合作为无新凭据的过渡方案 |

### DeepL

DeepL 的 `translate` API 支持：

- 明确的 `target_lang=ZH-HANS`；
- 不计费但会影响译文的 `context`，适合缺少上下文的短标题；
- `latency_optimized`、`quality_optimized` 和折中模式；
- glossary 和 tag handling；
- persistent HTTP connections，官方将其列为低延迟最佳实践。

建议传固定、非 Story 的 context，例如 `Cryptocurrency and financial-market news headline.`。不要把 Story、摘要或实体查询接回 Push。首版不建 glossary；只有离线评测证明相同术语反复出错时，再加入一个很小、代码审查过的词表。

官方 SDK会对部分 429/500 做重试，不符合本项目零重试决定，因此应直接用 `httpx.AsyncClient` 调 REST API。

来源：[DeepL Translate API](https://developers.deepl.com/api-reference/translate/request-translation)、[DeepL supported languages](https://developers.deepl.com/docs/getting-started/supported-languages)、[DeepL pre-production checklist](https://developers.deepl.com/docs/best-practices/pre-production-checklist)、[DeepL usage limits](https://developers.deepl.com/docs/resources/usage-limits)、[DeepL API 数据处理说明](https://www.deepl.com/en/privacy)。

### 阿里云

阿里云当前 `TranslateGeneral` 有效，限制为 50 QPS、单请求 5,000 字符，并支持可选 `Context`。专业版 `Translate` 支持 `finance` 场景，更贴近 Tracefold，而 `title` 实际是电商标题引擎，不应仅因为名字相同就选择。当前每月流量远低于容量限制。

代价是 OpenAPI RPC 签名和 RAM AccessKey 管理明显比 DeepL 单 key REST 更重。若使用 SDK，必须确认或显式关闭内部自动重试；若 SDK 无法保证单次调用，应使用最小的官方签名客户端封装，但不要在业务层加入重试。

按官方当前价格粗算：月输入 543 万字符，扣除每月 100 万免费额度后，通用版约为 `443 万 × ¥50/百万 = ¥221.5/月`；专业版约 `¥265.8/月`。这是用量估算，不含税费、促销或账号差异。

来源：[阿里云 TranslateGeneral](https://help.aliyun.com/zh/machine-translation/developer-reference/api-reference-machine-translation-universal-version-call-guide)、[阿里云专业版 Translate](https://help.aliyun.com/en/machine-translation/developer-reference/machine-translation-professional-call-guide)、[阿里云 API 概览](https://help.aliyun.com/zh/machine-translation/developer-reference/api-overview-1)、[阿里云价格](https://help.aliyun.com/zh/machine-translation/product-overview/pricing-of-machine-translation)。

### 百度

百度通用文本翻译使用一个 HTTPS endpoint、form body 和 `appid + query + salt + secret` 的 MD5 签名，接入面比阿里 RPC 小。高级版 QPS 10，远高于当前串行 Push 的需要，并支持术语干预。

按官方当前价格粗算：高级版扣除每月 100 万免费额度后，约为 `443 万 × ¥49/百万 = ¥217.1/月`。是否选它应由盲评质量和真实节点延迟决定，不能仅凭价格或大陆网络假设决定。

来源：[百度通用文本翻译接入文档](https://fanyi-api.baidu.com/product/113)、[百度服务开通与价格](https://fanyi-api.baidu.com/access/0/1)。

### Google、Azure、AWS

Google Cloud Translation Basic 是这三者中最接近 KISS 的选项：API key + v2 translate endpoint。Advanced 的 glossary、区域 endpoint 和 IAM 会扩大配置面，除非实测术语错误证明必要，否则不值得。按 Google 官方标准 NMT 价格，约 543 万字符/月扣除 50 万免费额度后约为 `$98.6/月`。

Azure Translator 的 REST 也很直接并支持 dynamic dictionary，但需要额外 Azure 资源/区域配置；其容器方案官方建议的资源量约 12–16 GB RAM、4 CPU，明显不适合当前 2 GB/2 CPU Worker。AWS Translate 需要 SigV4/IAM/区域配置，当前没有足够收益抵消这部分运维面。

来源：[Google Cloud Translation API overview](https://docs.cloud.google.com/translate/docs/api-overview)、[Google pricing](https://cloud.google.com/products/translate/pricing)、[Google data usage](https://docs.cloud.google.com/translate/data-usage)、[Azure Translator REST](https://learn.microsoft.com/en-us/rest/api/translator/translator/translate?view=rest-translator-v3.0)、[Azure Translator container](https://learn.microsoft.com/en-us/azure/ai-services/translator/containers/overview)、[AWS Translate quotas](https://docs.aws.amazon.com/translate/latest/dg/what-is-limits.html)、[AWS custom terminology](https://docs.aws.amazon.com/translate/latest/dg/how-custom-terminology.html)。

### 腾讯云与本地模型

腾讯云历史价格页面仍显示文本翻译收费，但官方 API 更新历史已经宣布 `TextTranslate` 在 2026-07-08 删除、`TextTranslateBatch` 更早删除。应以当前 API 生命周期为准，排除该方案。

本地方案中，Helsinki OPUS-MT `en-zh` 是 Apache-2.0，能由 CTranslate2 转换和 INT8 量化；技术上可行。问题是它把供应商 SLA 换成了项目自身的模型分发、常驻内存、CPU 抢占、冷启动、语言检测、质量回归和模型升级。当前只有标题翻译、原文又是无条件 fallback，本地部署的收益不足。Meta NLLB-200 distilled 600M 官方模型卡还明确说明其为研究模型、非生产发布，并使用非商业许可，因此不能用于本产品生产路径。

来源：[腾讯云 API 更新历史](https://cloud.tencent.com/document/product/551/17231)、[腾讯云当前 API 概览](https://cloud.tencent.com/document/product/551/15612)、[OPUS-MT en-zh model card](https://huggingface.co/Helsinki-NLP/opus-mt-en-zh)、[CTranslate2 conversion](https://opennmt.net/CTranslate2/conversion.html)、[CTranslate2 performance](https://opennmt.net/CTranslate2/performance.html)、[NLLB-200 distilled 600M model card](https://huggingface.co/facebook/nllb-200-distilled-600M)。

## 推荐架构

```text
pending NewsItemPush
  -> 若已是中文：not_needed
  -> 否则调用唯一 Translator（异步、绝对 1.5 s、零重试）
       -> 成功且校验通过：translated
       -> 任何失败：fallback 原文
  -> 原子 pending -> sending，冻结 presentation
  -> 唯一一次 Feishu 发送
```

具体约束：

- Translator 接口改为异步；翻译网络调用不再进入共享 `FiniteOperations` 线程池。
- 每个 Push turn 仍串行处理一条，所以自然最多一个翻译请求在途；不新增 semaphore、令牌桶或专用调度节拍。
- 复用一个进程级 `httpx.AsyncClient` 以获得连接复用；关闭时正常 `aclose()`。
- deadline 是从调用开始到完整结果的绝对 wall-clock 期限，不依赖 socket read timeout。
- 只发送原始标题、目标语言和固定领域 context；不读取 Story，不传描述或用户数据。
- 不请求 JSON，不做聊天 messages，不允许总结/扩写；供应商响应只取译文字符串。
- 保留当前 `_validated_translation`：非空、长度、中文、数字/百分比/`$TOKEN`/大写符号锚点一致。
- 不记录原始供应商响应或凭据；继续只记录 outcome、duration 和 sanitized fallback code。
- 不缓存。provider item ID 本身已经去重，跨 Item 标题缓存命中收益不明确，却会引入失效和持久化问题。
- 不做翻译后的第二条消息或卡片更新；presentation 在 Feishu fence 前一次冻结。

## 供应商赛马与采用门槛

不要在生产发送路径做 shadow call。使用近期事实的离线、脱敏标题样本：

1. 从 Strategy `1018` 和 `1019` 各取 100 条，覆盖短标题、长标题、数字、百分比、金额、`$TOKEN`、大写 ticker、无链接和 OI/行情术语。
2. 从实际生产 Worker 所在网络串行调用 DeepL、阿里专业版 finance、百度高级版；每个标题每家只调用一次，不做 warm-up 之外的重试。
3. 记录 DNS/TLS/首字节/总耗时、HTTP/供应商错误、锚点校验结果和译文长度，不记录密钥。
4. 对至少 50 条做盲评：事实不增删、中文可读性、金融术语、主体/方向/数值是否准确。评审者不知道供应商。
5. 只有一个供应商进入生产；其他测试凭据和代码不进入运行时。

建议硬门槛：

- 真实上海 Worker 上总耗时 P95 不高于 1.0 秒，P99 不高于 1.5 秒；
- 连接/供应商错误率低于 1%；
- 锚点校验通过率至少 99%；
- 盲评没有事实方向、主体、金额、百分比或 ticker 的严重错误；
- 预计月成本和数据处理条款由 operator 接受。

若没有供应商同时通过门槛，正确行为不是增加 waterfall 或重试，而是保留原文，继续使用止血版或关闭翻译。

## 对 Issue #42 的影响

Issue #42 的核心边界保持不变：Item Push 与 Story 解耦、翻译 best-effort、原文 fallback、零重试、翻译不影响 health、presentation 在 Feishu fence 前冻结。

需要显式修订的一处是 Issue 当前的 KISS stop rule：它要求复用全局 LLM 配置且禁止 Push-specific translation credentials。专用翻译 API 不应冒充 Brief LLM 配置；更清晰的做法是增加一个很小的全局可选 `translation` 配置，或明确的 `news.push.translation_api_key`，并且只允许一个代码固定的供应商。这个配置变化必须先写回 Issue，再实现，不能静默突破规格。

不建议修改的部分：三秒仍可作为产品上界，但适配器自身应在 1.5 秒绝对 deadline 截止；剩余时间留给调度和清理。翻译失败仍不降级 Push health；Feishu 仍为零重试。

## 最终建议

1. 立即可做：把当前 DeepSeek translator 改成 async、纯文本短输出和 1.5 秒绝对 deadline，解除共享线程风险；不改变产品与持久化模型。
2. 随后离线赛马 DeepL、阿里专业版 finance、百度高级版。
3. DeepL 通过上海网络与质量门槛时固定选 DeepL；否则固定选阿里专业版 finance。
4. 对 Issue #42 做一次很小的规格修订：允许一个独立翻译凭据；仍禁止供应商选择器、fallback chain、重试、队列和新状态机。
5. 若赛马无明确赢家，维持原文优先；“不翻译”比引入一套复杂且长尾不可靠的翻译子系统更符合 KISS。
