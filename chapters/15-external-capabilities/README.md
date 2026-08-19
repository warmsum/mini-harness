# 15｜网络搜索与网页抓取

> 预计时间：55 分钟 ｜ 前置：完成第 02 章 ｜ 本章调用真实 DeepSeek Web Search 与真实网络

模型的内置知识受训练数据时间范围限制，无法保证版本号、价格和新闻等信息仍然有效。遇到这类问题时，Agent 需要通过外部能力搜索网络或读取网页。本章实现两个真实网络工具，并解释 DeepSeek Web Search 的调用方式。

一个容易产生的误解是，DeepSeek 没有搜索专用端点，找不到一个 POST /search 接口。官方 web-search-deepseek 的做法是：把搜索做成一次完整的模型调用，调用 Anthropic 兼容的 /messages 端点，并携带一个 web_search_20250305 服务器工具；服务器侧执行真正的搜索，把结构化结果作为内容块返回。官方文档第 11 行写明代价：一次搜索会产生完整模型轮次的延迟与 token 开销，比纯检索端点更重。

## 学习目标

完成本章后，你将能够：

- 区分专用搜索端点、模型内服务器工具和直接抓取网页；
- 调用 DeepSeek 的 Anthropic 兼容 `/messages` 端点完成搜索；
- 从结构化内容块中提取回答与来源，而不是从正文猜测 URL；
- 使用 `web_fetch` 获取网页，并对正文进行清理和截断。

## 15.1 原理：搜索的三种形态

联网搜索常见的实现方式有三种，各有取舍：

| 形态 | 代表 | 取舍 |
|------|------|------|
| 专用搜索端点 | Exa、Perplexity | 快、便宜，但要额外服务商与密钥 |
| 模型内服务器工具 | DeepSeek web_search | 慢，一次模型轮次，但零新依赖、复用模型密钥 |
| 自己抓网页 | 通用爬虫 | 最灵活，但要处理反爬、HTML 解析 |

官方选了中间档：复用 DEEPSEEK_API_KEY，不增加密钥，走 Anthropic 兼容基址 https://api.deepseek.com/anthropic/v1。这个基址不是 chat-completions 的 https://api.deepseek.com，两个协议两个基址，官方文档第 15 行专门强调不复用 DEEPSEEK_BASE_URL。模型名是 deepseek-v4-flash，请求头带 anthropic-version: 2023-06-01，对应官方配置表的默认值。

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

它与第 01 章的 chat 接口有三处协议差异：

1. 端点：/anthropic/v1/messages，Anthropic 兼容，不是 /chat/completions；
2. 认证头：x-api-key，不是 Authorization: Bearer；
3. 工具声明：Anthropic 服务器工具格式，type 为 web_search_20250305，max_uses 是服务器最多搜几次的上限，官方默认 5，教学版默认 3。

## 15.3 解析：结构化来源与严格模式

响应的 content 是块列表，两种块与本章有关：

- web_search_tool_result 块是结构化搜索结果。每块内含若干 web_search_result 条目，这就是来源清单，从块里拿，绝不从模型文本里抓 URL。官方第 11 行写明：提供方解析这些块，绝不会从模型文本中抓取 URL。
- text 块是模型基于搜索结果生成的回答正文。

解析时有一个官方的严格模式决策：

```python
        if not sources:
            raise RuntimeError(
                "[WEB_PROVIDER_ERROR] 响应中没有 web_search_tool_result 块"
            )
```

如果响应中没有搜索结果块，例如模型没有触发搜索而是直接回答，客户端就报告错误，不能把普通模型文本当作搜索结果。否则调用方无法判断内容是否真的来自网络。此外，来源按 URL 去重，因为一次请求中的多次搜索可能返回同一页面；官方也采用相同处理。

当前 API 的搜索摘要字段是 encrypted_content，一段版权保护格式的密文，客户端拿不到明文摘要。可读的摘录在文本块的 citations 字段里，官方第 41 行写明 cited_text 条目按 URL 标识、单独位于文本块的 citations 中。教学版如实处理：来源清单用 title 与 url，回答用 text 块。

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

教学版使用正则提取 title、移除标签，并截取前 800 个字符。真实产品通常会使用正文提取库处理复杂页面，但基本流程仍是 GET、解析、提取和截断。网页可能包含数 MB 内容，限制返回长度可以避免单次抓取占用过多上下文。

## 15.5 运行完整示例

```bash
uv run python chapters/15-external-capabilities/src/demo.py
```

真实输出，搜索结果随时间变化，结构稳定：

```
=== ① Web Search：真实搜索 DeepSeek Harness ===
  来源（13 条）：
  - DeepSeek Harness 怎么下载？官方渠道、安装包与源码获取全指南
    https://www.ai-indeed.com/encyclopedia/29694.html
  - deepseek-harness/README.zh.md at 47f943859bef60e4160492346772ded9b24f765a
    https://github.com/deepseek-ai/DeepSeek-Harness/blob/HEAD/README.zh.md
  ...（共 13 条）

  模型基于搜索结果的回答（节选）：
  # DeepSeek Harness 是什么？

  **DeepSeek Harness**（简称 **dsh**）是 **DeepSeek AI** 于 2026 年 8 月
  13 日发布并开源的 **Agent 运行框架（agent harness）**，首个版本为 v0.1
  开发者预览版。
  ...

=== ② web_fetch：真实抓取网页 ===
  标题: GitHub - deepseek-ai/deepseek-harness: DeepSeek Harness: Everything is a Plugin. · GitHub

  正文片段: GitHub - deepseek-ai/deepseek-harness: DeepSeek Harness: ...
  …（正文片段截断于 800 字符）
```

两个观察点：① 来源来自结构化结果块，title 与 URL 成对出现，回答正文则由模型基于搜索结果生成；② web_fetch 读取真实 HTML，再提取标题与正文片段。

## 本章小结

- `WebSearchClient`：Anthropic 兼容 /messages 端点、服务器工具声明、结构化块解析、严格模式、按 URL 去重
- `web_fetch`：真实 GET、标题与正文提取、返回长度限制
- 搜索三形态光谱与官方的中间档选择

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/web/web-search-deepseek/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/web/web-search-deepseek/README.zh.md) | `WebSearchClient` | Anthropic 兼容端点与服务器工具在第 5 行；完整模型轮次代价在第 11 行；严格模式在第 13 行；基址与密钥复用在第 15 行；模型名与 maxUses 在第 24、27 行；来源映射与 citations 在第 41 行；按 URL 去重在第 43 行 |
| [`packages/web/tool-web/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/web/tool-web/README.zh.md) | `web_fetch` | 官方工具层管理搜索与抓取的模型面，schema、渲染、日志，教学版合并为两个函数 |
| 同上，凭据 seam | `load_api_key` | 官方经 ctx.credentials 每次搜索解析凭据，轮换密钥无需重启；教学版每次调用读 .env，效果等价 |

## 练习

1. **严格模式验证。** 把查询改成 1+1 等于几这类模型不会触发搜索的问题，观察 WEB_PROVIDER_ERROR 的抛出路径，理解宁可报错也不拿猜的当搜的。
2. **max_uses 实验。** 把 max_uses 设为 1 与 5 各跑一次，对比来源数量与回答详略，理解这个参数对成本与质量的影响。
3. **抓取失败处理。** web_fetch 一个 404 页面与一个超时域名，观察 raise_for_status 与超时异常；给 web_fetch 加上把失败转成给模型看的错误文本的包装，呼应第 02 章错误回灌。
4. **搜索进 Agent。** 把 `WebSearchClient.search` 包装成第 02 章风格的 Tool，挂进第 07 章的 Agent，让模型在真实对话里决定何时该搜、搜什么。
