# 15｜外部能力：真实的网络搜索与网页抓取

> 预计时间：55 分钟 ｜ 前置：完成第 02 章 ｜ 本章调用真实 DeepSeek Web Search 与真实网络

模型的知识有截止日期，它的世界到训练数据为止。凡是「最新」类
问题——版本号、价格、新闻——模型只能猜。Agent 要突破这堵墙，
需要**外部能力**：联网搜索、抓取网页。本章实现两个真实的网络
工具，并讲清官方 Web Search 的一个反直觉实现。

先消除一个可能的误解：DeepSeek **没有**「搜索专用端点」——你
不会找到一个 `POST /search` 接口。官方 `web-search-deepseek` 的
做法是：把搜索做成一次**完整的模型调用**，调用 Anthropic 兼容
的 `/messages` 端点，并携带一个 `web_search_20250305` 服务器
工具；服务器侧执行真正的搜索，把结构化结果作为内容块返回。
官方文档的原话：「一次搜索会产生完整模型轮次的延迟与 token
开销，比纯检索端点更重」。

## 15.1 原理：搜索的三种形态

先建立「联网搜索」的实现光谱，三档各有取舍：

| 形态 | 代表 | 取舍 |
|------|------|------|
| 专用搜索端点 | Exa、Perplexity | 快、便宜，但要额外服务商与密钥 |
| 模型内服务器工具 | DeepSeek web_search | 慢（一次模型轮次），但零新依赖、复用模型密钥 |
| 自己抓网页 | 通用爬虫 | 最灵活，但要处理反爬、HTML 解析 |

官方选了中间档：复用 `DEEPSEEK_API_KEY`（「不增加密钥」），
走 Anthropic 兼容基址 `https://api.deepseek.com/anthropic/v1`
（**不是** chat-completions 的 `https://api.deepseek.com`——
两个协议两个基址，官方文档专门强调不能混用）。模型名是
`deepseek-v4-flash`，请求头带 `anthropic-version: 2023-06-01`。

## 15.2 WebSearchClient：一次携带服务器工具的模型调用

```python
class WebSearchClient:
    def __init__(self, api_key=None, base_url=SEARCH_BASE_URL, model=SEARCH_MODEL):
        if api_key is None:
            from client import load_api_key
            api_key = load_api_key()
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def search(self, query: str, max_uses: int = 3) -> WebSearchResult:
        response = httpx.post(
            f"{self.base_url}/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": query}],
                "tools": [
                    {"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": max_uses}
                ],
            },
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        # ...解析内容块
```

与第 01 章 chat 的三处协议差异，值得对照记忆：

1. **端点**：`/anthropic/v1/messages`（Anthropic 兼容），不是
   `/chat/completions`（OpenAI 兼容）；
2. **认证头**：`x-api-key`，不是 `Authorization: Bearer`；
3. **工具声明**：Anthropic 服务器工具格式
   `{"type": "web_search_20250305", "name": "web_search", "max_uses": N}`，
   其中 `max_uses` 是「服务器最多搜几次」的上限（官方默认 5）。

## 15.3 解析：结构化来源与严格模式

响应的 `content` 是块列表，两种块与我们有关：

- **`web_search_tool_result` 块**：结构化搜索结果。每块内含
  若干 `{type: "web_search_result", title, url, ...}` 条目——
  **这就是来源清单，从块里拿，绝不从模型文本里抓 URL**（官方
  明确：提供方解析这些块，「绝不会从模型文本中抓取 URL」）。
- **`text` 块**：模型基于搜索结果生成的回答正文。

解析时有一个官方的**严格模式**决策：

```python
        if not sources:
            raise RuntimeError(
                "[WEB_PROVIDER_ERROR] 响应中没有 web_search_tool_result 块"
            )
```

响应里没有搜索结果块（比如模型没触发搜索、直接凭记忆回答），
就**报错**而不是把模型文本当搜索结果用——把「猜的」当「搜的」
比搜不到更危险。此外按 URL 去重（一次请求可能多次呈现同一
页面，官方同款处理）。

值得注意的一个真实细节：本章实测时发现当前 API 的摘要字段是
`encrypted_content`（一段密文）——DeepSeek 的版权保护格式，
客户端拿不到明文摘要。官方的可读摘录来自文本块的 `citations`
字段。教学版如实处理：来源清单用 title/url，回答用 text 块。

## 15.4 web_fetch：真实抓取网页

第二个工具更朴素——真实 HTTP GET 一个 URL，提取标题与正文
片段：

```python
def web_fetch(url: str, timeout_seconds: float = 20.0) -> str:
    response = httpx.get(url, timeout=timeout_seconds, follow_redirects=True)
    response.raise_for_status()
    html = response.text
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else "(无标题)"
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                     flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", cleaned)
    text = re.sub(r"\s+", " ", text).strip()
    return f"标题: {title}\n\n正文片段: {text[:800]}"
```

教学版用最朴素的手段：正则提 `<title>`、去标签、截 800 字符。
真实产品会用可读性提取库（readability）处理复杂的页面结构，
但核心流程一致：GET → 解析 → 提取 → 截断。**截断**不是偷懒，
是纪律——网页可能几 MB，Agent 的上下文装不下，也只需要概要。

## 15.5 跑一遍完整 demo

```bash
uv run python chapters/15-external-capabilities/src/demo.py
```

真实输出（搜索结果随时间变化，结构稳定）：

```
=== ① Web Search：真实搜索 DeepSeek Harness ===
  来源（13 条）：
  - DeepSeek Harness 怎么下载？官方渠道、安装包与源码获取全指南
    https://www.ai-indeed.com/encyclopedia/29694.html
  - 实测DeepSeek Harness！梁文锋憋的"黑色鲸鱼"大招，有惊喜
    https://www.zhidx.com/p/584897.html
  - GitHub - Lyowisee/deepseek-harness · GitHub
    https://github.com/Lyowisee/deepseek-harness
  ...（共 13 条）

  模型基于搜索结果的回答（节选）：
  # DeepSeek Harness（DSH）是什么？
  DeepSeek Harness 是 DeepSeek AI 官方开源的一款智能体运行框架，
  于 2026 年 8 月 13 日发布 v0.1 开发者预览版，并以 MIT 许可完全开源。

=== ② web_fetch：真实抓取网页 ===
  标题: GitHub - deepseek-ai/DeepSeek-Harness: ...
  正文片段: DeepSeek Harness ...
```

两个观察点：① 的来源是**结构化块**里的真数据（title + url
成对出现），回答是模型在搜索结果基础上写的——「搜索」与
「生成」分离；② 抓取的网页是活生生的真实 HTML。

## 15.6 本章小结：亲手写了什么

- `WebSearchClient`：Anthropic 兼容 /messages 端点 + 服务器
  工具声明 + 结构化块解析 + 严格模式 + 按 URL 去重
- `web_fetch`：真实 GET + 标题/正文提取 + 截断纪律
- 搜索三形态光谱与官方的中间档选择

## 15.7 对照官方 DSH

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/web/web-search-deepseek/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/web/web-search-deepseek/README.zh.md) | `WebSearchClient` | 官方 Anthropic Messages API + web_search_20250305 工具、复用 DEEPSEEK_API_KEY、严格模式（无结果块即 WEB_PROVIDER_ERROR）、按 URL 去重——与本章一一对应 |
| [`packages/web/tool-web/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/web/tool-web/README.zh.md) | `web_fetch` | 官方工具层管理搜索与抓取的模型面（schema、渲染、日志），教学版合并为两个函数 |
| 同上（官方凭据 seam） | `load_api_key` | 官方经 `ctx.credentials` 每次搜索解析凭据（换密钥无需重启）；教学版每次调用读 .env，效果等价 |

## 15.8 练习

1. **严格模式验证**：把查询改成「1+1 等于几」这类模型不会触发
   搜索的问题，观察 WEB_PROVIDER_ERROR 的抛出路径，理解「宁可
   报错也不拿猜的当搜的」。
2. **max_uses 实验**：把 max_uses 设为 1 与 5 各跑一次，对比
   来源数量与回答详略，理解这个参数对成本与质量的影响。
3. **抓取失败处理**：web_fetch 一个 404 页面与一个超时域名，
   观察 raise_for_status 与超时异常；给 web_fetch 加上把失败
   转成「给模型看的错误文本」的包装（呼应第 02 章错误回灌）。
4. **搜索进 Agent**：把 `WebSearchClient.search` 包装成第 02 章
   风格的 Tool，挂进第 07 章的 Agent，让模型在真实对话里决定
   「何时该搜、搜什么」（需要 .env）。
