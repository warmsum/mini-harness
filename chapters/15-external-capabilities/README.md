# 15｜网络搜索与网页抓取

> 预计时间：55 分钟 ｜ 前置：完成第 02 章 ｜ 本章调用真实 DeepSeek Web Search 与真实网络

模型的内置知识受训练数据时间范围限制，无法保证版本号、价格和新闻等信息仍然有效。遇到这类问题时，Agent 需要通过外部能力搜索网络或读取网页。本章实现两个真实网络能力，并解释 DeepSeek Web Search 从单条 provider 请求到多查询工具调用的完整数据流。

一个容易产生的误解是，DeepSeek 没有搜索专用端点，找不到一个 `POST /search` 接口。官方 web-search-deepseek 的做法是：把搜索做成一次完整的模型调用，调用 Anthropic 兼容的 `/messages` 端点，并携带一个 `web_search_20250305` 服务器工具；服务器侧执行真正的搜索，把结构化结果作为内容块返回。代价是一次搜索会产生完整模型轮次的延迟与 token 开销，比纯检索端点更重。

## 学习目标

完成本章后，你将能够：

- 区分专用搜索端点、模型内服务器工具和直接抓取网页；
- 调用 DeepSeek 的 Anthropic 兼容 `/messages` 端点完成搜索；
- 接收必填 `queries` 数组，并发搜索、去重并按排名轮询合并来源；
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

## 15.2 Provider 层：一条查询就是一次模型调用

官方把两层职责分开。`web-search-deepseek` provider 每次只接收一条 `query`，负责调用 DeepSeek 并返回统一的 `WebSearchResult`。`tool-web` 才负责接收模型给出的 `queries` 数组、并发调用 provider 和合并结果。本章用 `_search_one()` 和 `search()` 表达同样的边界。

```python
class WebSearchClient:
    def _search_one(self, query, max_results, max_uses) -> WebSearchResult:
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

它与第 01 章的 chat 接口有四处协议差异：

1. 端点：/anthropic/v1/messages，Anthropic 兼容，不是 /chat/completions；
2. 认证头：按 DeepSeek 当前兼容要求同时发送 `x-api-key` 与 Bearer；
3. 工具声明：Anthropic 服务器工具格式，type 为 `web_search_20250305`，`max_uses` 是服务器最多搜几次的上限，默认 5；
4. 结果数量：DeepSeek 没有 `max_results` 请求参数，provider 在 URL 去重后自行截断，并用 `truncated` 告诉调用方还有来源未返回。

## 15.3 工具层：`queries` 数组与轮询合并

rc.8 的 `web_search` 不再接收单个 `query`，而是接收必填的 `queries` 数组。数组可含 1 到 4 条查询；只搜一次也必须写成单元素数组。边界检查发生在去重之前，所以传入 5 条重复查询仍然超限，不能借去重绕过查询预算。

```python
def search(self, queries, max_results=5, max_uses=5, max_queries=4):
    if not queries:
        raise ValueError("queries 至少需要一条查询")
    if len(queries) > max_queries:
        raise ValueError(f"queries 最多只能有 {max_queries} 条查询")
    if any(not isinstance(q, str) or not q.strip() for q in queries):
        raise ValueError("queries 中的每一项都必须是非空字符串")

    unique_queries = list(dict.fromkeys(queries))
    # 单条直接调用；多条通过 ThreadPoolExecutor 并发调用 _search_one
```

精确重复的查询只执行第一次。多条查询完成后，来源不是简单首尾相接，而是按排名轮询：先取每条查询的第 1 个结果，再取每条查询的第 2 个结果，同时按 URL 去重，最后应用整批的 `max_results`。这样一条结果很多的查询不会挤掉其他查询。任一查询失败时，整批搜索返回错误并丢弃已经成功的结果；教学版能取消尚未开始的 future，但同步 HTTP 请求无法中断已经在运行的线程，官方则会通过共享取消信号终止兄弟请求并等待全部结算。

## 15.4 解析：结构化来源与严格模式

响应的 content 是块列表，两种块与本章有关：

- `web_search_tool_result` 块是结构化搜索结果。每块内含若干 `web_search_result` 条目，这就是来源清单，从块里拿，绝不从模型文本里抓 URL。
- `text` 块可能包含提供方生成的回答，同时也带有 `citations`。回答正文不作为可信结果返回；这里只按 URL 取 `cited_text`，作为对应来源的 snippet。

解析时有一个官方的严格模式决策：

```python
        if not found_result_block:
            raise RuntimeError(
                "[WEB_PROVIDER_ERROR] 响应中没有 web_search_tool_result 块"
            )
```

如果响应中没有搜索结果块，例如模型没有触发搜索而是直接回答，客户端就报告错误，不能把普通模型文本当作搜索结果。否则调用方无法判断内容是否真的来自网络。此外，来源按 URL 去重，因为一次请求中的多次搜索可能返回同一页面；官方也采用相同处理。

当前 API 的搜索结果还可能带有 `encrypted_content`，客户端不能把它当明文摘要。可读摘录来自文本块的 `citations`：`cited_text` 按 URL 关联到相应来源。`page_age` 则映射为 `published_at`。最终 `WebSearchResult` 只有 `sources` 与 `truncated`，没有模型 answer 字段。

## 15.5 web_fetch：真实抓取网页

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

## 15.6 运行完整示例

```bash
uv run python chapters/15-external-capabilities/src/demo.py
```

真实输出，搜索结果随时间变化，结构稳定：

```
=== ① Web Search：真实搜索 DeepSeek Harness ===
  queries: ["DeepSeek Harness 是什么？", "DeepSeek Harness 官方仓库地址"]
  来源（5 条）：
  - GitHub - deepseek-ai/DeepSeek-Harness
    https://github.com/deepseek-ai/DeepSeek-Harness
    DeepSeek Harness is an open-source agent harness…
  - DeepSeek Harness documentation
    https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/README.zh.md
  ...
  是否因 max_results 截断: True 或 False

=== ② web_fetch：真实抓取网页 ===
  标题: GitHub - deepseek-ai/deepseek-harness: DeepSeek Harness: Everything is a Plugin. · GitHub

  正文片段: GitHub - deepseek-ai/deepseek-harness: DeepSeek Harness: ...
  …（正文片段截断于 800 字符）
```

三个观察点：① 两条查询并发执行，来源按排名轮询合并并按 URL 去重；② 来源来自结构化结果块，title、URL、可选 snippet 与发布时间一起返回，提供方生成的自由文本没有进入结果；③ `web_fetch` 读取真实 HTML，再提取标题与正文片段。

## 15.7 进入 Capstone

第 17 章注册 `web_search` 和 `web_fetch` 两个独立工具。前者接收 `queries[]`，通过 DeepSeek Anthropic Messages 服务器工具发现结构化来源，会产生模型 API 用量；后者只对指定 URL 执行普通 HTTP GET，不调用模型。搜索结果不会自动触发抓取，是否继续读取某个来源由 Agent 下一 step 决定。

## 本章小结

- `WebSearchClient._search_one`：Anthropic 兼容 `/messages` 端点、服务器工具声明、结构化块解析与严格模式
- `WebSearchClient.search`：必填 queries 数组、先校验后去重、并发搜索、URL 去重、轮询合并与整批截断
- `web_fetch`：真实 GET、标题与正文提取、返回长度限制
- 搜索三形态光谱与官方的中间档选择

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/web/tool-web/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/web/tool-web/README.zh.md) | `WebSearchClient.search` | 对齐必填 `queries`、最多 4 条、先校验后去重、并发调用、URL 去重、轮询合并与整批失败；教学版不能中断已运行的同步 HTTP 线程 |
| [`packages/web/web-search-deepseek/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/web/web-search-deepseek/README.zh.md) | `_search_one` | 对齐 Anthropic Messages、服务器工具、严格模式、来源映射、citations、`maxResults` 与拒绝重定向 |
| [`packages/web/web-fetch-http/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/web/web-fetch-http/README.zh.md) | `web_fetch` | 教学版只保留 GET、文本清理与截断；官方还有完整的传输卫生和资源上限 |
| 官方 credentials seam | `load_api_key` | 官方每次搜索重新解析凭据与 settings；教学版在 `WebSearchClient` 初始化时读取一次，轮换后要新建 client |

## 练习

1. **严格模式验证。** 把 queries 改成 `["1+1 等于几"]` 这类模型可能不会触发搜索的问题，观察 WEB_PROVIDER_ERROR 的抛出路径，理解宁可报错也不拿猜的当搜的。
2. **多查询实验。** 分别传入一个查询、两个互补查询和两个完全相同的查询，观察调用次数、来源顺序和去重结果；再把 max_uses 设为 1 与 5，对比它和 max_queries 两层预算的区别。
3. **抓取失败处理。** web_fetch 一个 404 页面与一个超时域名，观察 raise_for_status 与超时异常；给 web_fetch 加上把失败转成给模型看的错误文本的包装，呼应第 02 章错误回灌。
4. **搜索进 Agent。** 把 `WebSearchClient.search` 包装成第 02 章风格的 Tool，挂进第 07 章的 Agent，让模型在真实对话里决定何时该搜、搜什么。
