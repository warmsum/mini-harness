# 15｜网络搜索与网页抓取

> 预计时间：55 分钟 ｜ 前置：完成第 02 章 ｜ 本章调用真实 DeepSeek Web Search 与真实网络

模型的内置知识受训练数据时间范围限制，无法保证版本号、价格和新闻等信息仍然有效。遇到这类问题时，Agent 需要通过外部能力搜索网络或读取网页。本章实现两个真实网络工具，并解释 DeepSeek Web Search 的调用方式。

一个容易产生的误解是，DeepSeek 没有搜索专用端点，找不到一个 `POST /search` 接口。官方 web-search-deepseek 的做法是：把搜索做成一次完整的模型调用，调用 Anthropic 兼容的 `/messages` 端点，并携带一个 `web_search_20250305` 服务器工具；服务器侧执行真正的搜索，把结构化结果作为内容块返回。代价是一次搜索会产生完整模型轮次的延迟与 token 开销，比纯检索端点更重。

## 学习目标

完成本章后，你将能够：

- 区分专用搜索端点、模型内服务器工具和直接抓取网页；
- 调用 DeepSeek 的 Anthropic 兼容 `/messages` 端点完成搜索；
- 从结构化内容块中提取来源、摘录和发布日期，不信任提供方生成的自由文本；
- 使用 `web_fetch` 获取网页，并对正文进行清理和截断。

## 15.1 原理：搜索的三种形态

联网搜索常见的实现方式有三种，各有取舍：

| 形态 | 代表 | 取舍 |
|------|------|------|
| 专用搜索端点 | Exa、Perplexity | 快、便宜，但要额外服务商与密钥 |
| 模型内服务器工具 | DeepSeek web_search | 慢，一次模型轮次，但零新依赖、复用模型密钥 |
| 自己抓网页 | 通用爬虫 | 最灵活，但要处理反爬、HTML 解析 |

官方选了中间档：复用 `DEEPSEEK_API_KEY`，不增加密钥，走 Anthropic 兼容基址 `https://api.deepseek.com/anthropic/v1`。这个基址不是 chat-completions 的 `https://api.deepseek.com`，两个协议使用两个基址，不能复用 `DEEPSEEK_BASE_URL`。模型名是 `deepseek-v4-flash`，请求头带 `anthropic-version: 2023-06-01`。

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

    def search(
        self, query: str, max_results: int = 5, max_uses: int = 5
    ) -> WebSearchResult:
        response = httpx.post(
            f"{self.base_url}/messages",
            headers={
                "x-api-key": self.api_key,
                "authorization": f"Bearer {self.api_key}",
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 4096,
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": f"Perform a web search for the query: {query}",
                    }],
                }],
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

它与第 01 章的 chat 接口有三处协议差异：

1. 端点：/anthropic/v1/messages，Anthropic 兼容，不是 /chat/completions；
2. 认证头：按 DeepSeek 当前兼容要求同时发送 `x-api-key` 与 Bearer；
3. 工具声明：Anthropic 服务器工具格式，type 为 `web_search_20250305`，`max_uses` 是服务器最多搜几次的上限，默认 5；
4. 结果数量：DeepSeek 没有 `max_results` 请求参数，客户端在 URL 去重后自行截断，并用 `truncated` 告诉调用方还有来源未返回。

## 15.3 解析：结构化来源与严格模式

响应的 content 是块列表，两种块与本章有关：

- `web_search_tool_result` 块是结构化搜索结果。每块内含若干 `web_search_result` 条目，这就是来源清单，从块里拿，绝不从模型文本里抓 URL。
- `text` 块可能包含提供方生成的回答，同时也带有 `citations`。回答正文不作为可信结果返回；这里只按 URL 取 `cited_text`，作为对应来源的 snippet。

解析时有一个官方的严格模式决策：

```python
        if not sources:
            raise RuntimeError(
                "[WEB_PROVIDER_ERROR] 响应中没有 web_search_tool_result 块"
            )
```

如果响应中没有搜索结果块，例如模型没有触发搜索而是直接回答，客户端就报告错误，不能把普通模型文本当作搜索结果。否则调用方无法判断内容是否真的来自网络。此外，来源按 URL 去重，因为一次请求中的多次搜索可能返回同一页面；官方也采用相同处理。

当前 API 的搜索结果还可能带有 `encrypted_content`，客户端不能把它当明文摘要。可读摘录来自文本块的 `citations`：`cited_text` 按 URL 关联到相应来源。`page_age` 则映射为 `published_at`。最终 `WebSearchResult` 只有 `sources` 与 `truncated`，没有模型 answer 字段。

## 15.4 web_fetch：真实抓取网页

第二个工具更朴素，真实 HTTP GET 一个 URL，提取标题与正文片段：

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

教学版使用正则提取 title、移除标签，并截取前 800 个字符。它只用于演示“获取、清理、截断”这条数据流，并不是生产安全抓取器。

官方 `web-fetch-http` 还会校验 URL 协议和凭据、限制 URL/响应字节/解码字符、只跟随同源重定向、拒绝二进制内容并传播取消信号；它也明确说明当前没有 SSRF/私网防护。教学版没有实现这些传输防护，还允许跨源重定向，因此不要对不可信 URL 使用它，也不要把它部署到能访问敏感内网的环境。

## 15.5 运行完整示例

```bash
uv run python chapters/15-external-capabilities/src/demo.py
```

真实输出，搜索结果随时间变化，结构稳定：

```
=== ① Web Search：真实搜索 DeepSeek Harness ===
  来源（5 条）：
  - GitHub - deepseek-ai/DeepSeek-Harness
    https://github.com/deepseek-ai/DeepSeek-Harness
    DeepSeek Harness is an open-source agent harness…
  - DeepSeek Harness documentation
    https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/README.zh.md
  ...
  是否因 max_results 截断: True 或 False

=== ② web_fetch：真实抓取网页 ===
  标题: GitHub - deepseek-ai/deepseek-harness: DeepSeek Harness: Everything is a Plugin. · GitHub

  正文片段: GitHub - deepseek-ai/deepseek-harness: DeepSeek Harness: ...
  …（正文片段截断于 800 字符）
```

两个观察点：① 来源来自结构化结果块，title、URL、可选 snippet 与发布时间一起返回；提供方生成的自由文本没有进入结果。② `web_fetch` 读取真实 HTML，再提取标题与正文片段。

## 本章小结

- `WebSearchClient`：Anthropic 兼容 `/messages` 端点、服务器工具声明、结构化块解析、严格模式、按 URL 去重与结果截断
- `web_fetch`：真实 GET、标题与正文提取、返回长度限制
- 搜索三形态光谱与官方的中间档选择

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/web/web-search-deepseek/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/web/web-search-deepseek/README.zh.md) | `WebSearchClient` | 对齐 Anthropic Messages、服务器工具、严格模式、来源映射、citations、URL 去重、`maxResults` 与拒绝重定向 |
| [`packages/web/web-fetch-http/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/web/web-fetch-http/README.zh.md) | `web_fetch` | 教学版只保留 GET、文本清理与截断；官方还有完整的传输卫生和资源上限 |
| 官方 credentials seam | `load_api_key` | 官方每次搜索重新解析凭据与 settings；教学版在 `WebSearchClient` 初始化时读取一次，轮换后要新建 client |

## 练习

1. **严格模式验证。** 把查询改成 1+1 等于几这类模型不会触发搜索的问题，观察 WEB_PROVIDER_ERROR 的抛出路径，理解宁可报错也不拿猜的当搜的。
2. **max_uses 实验。** 把 max_uses 设为 1 与 5 各跑一次，对比来源数量与摘录覆盖，理解这个参数对成本与质量的影响。
3. **抓取失败处理。** web_fetch 一个 404 页面与一个超时域名，观察 raise_for_status 与超时异常；给 web_fetch 加上把失败转成给模型看的错误文本的包装，呼应第 02 章错误回灌。
4. **搜索进 Agent。** 把 `WebSearchClient.search` 包装成第 02 章风格的 Tool，挂进第 07 章的 Agent，让模型在真实对话里决定何时该搜、搜什么。
