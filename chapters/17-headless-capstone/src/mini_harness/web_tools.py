"""第 15 章：真实的外部能力 —— Web Search 与网页抓取。

对应官方 packages/web/tool-web、web-search-deepseek 与 web-fetch-http。
官方实现的真相（web-search-deepseek/README.zh.md）：
DeepSeek 没有专用搜索端点，Web Search 是一次携带 web_search
服务器工具的「Anthropic 兼容 Messages API」完整模型调用——
服务器侧执行搜索，返回结构化 web_search_tool_result 块。

本章实现两个真实工具：
1. WebSearchClient —— 走 https://api.deepseek.com/anthropic/v1/messages；
2. web_fetch —— 真实 HTTP GET 一个 URL，提取标题与正文片段。
"""

from __future__ import annotations

import re
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import cast

import httpx

# 官方默认值（web-search-deepseek 配置表）
SEARCH_BASE_URL = "https://api.deepseek.com/anthropic/v1"
SEARCH_MODEL = "deepseek-v4-flash"
ANTHROPIC_VERSION = "2023-06-01"
WEB_SEARCH_TOOL = "web_search_20250305"
SEARCH_MAX_QUERIES = 4


@dataclass(frozen=True)
class WebSource:
    """一条搜索来源。"""

    title: str
    url: str
    snippet: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class WebSearchResult:
    """一条查询或一批查询合并后的结果（官方 WebSearchResult 的简化版）。"""

    sources: tuple[WebSource, ...]
    truncated: bool = False


class WebSearchClient:
    """DeepSeek Web Search 客户端。

    与第 01 章的 chat 是两套协议：这里走 Anthropic 兼容的
    /messages 端点（不是 chat/completions），密钥复用同一个
    DEEPSEEK_API_KEY（官方明确不增加密钥）。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = SEARCH_BASE_URL,
        model: str = SEARCH_MODEL,
    ) -> None:
        if api_key is None:
            from .client import load_api_key

            api_key = load_api_key()
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def search(
        self,
        queries: list[str],
        max_results: int = 5,
        max_uses: int = 5,
        max_queries: int = SEARCH_MAX_QUERIES,
    ) -> WebSearchResult:
        """并发执行一到多条查询，再合并为一份结构化结果。

        官方的 provider seam 每次仍只接收一个 query；rc.8 的模型面
        web_search 工具改为接收必填 queries 数组，并在工具层完成并发与合并。
        """
        if max_queries <= 0:
            raise ValueError("max_queries 必须是正整数")
        if not queries:
            raise ValueError("queries 至少需要一条查询")
        if len(queries) > max_queries:
            raise ValueError(f"queries 最多只能有 {max_queries} 条查询")
        if any(not isinstance(query, str) or not query.strip() for query in queries):
            raise ValueError("queries 中的每一项都必须是非空字符串")
        if max_results <= 0 or max_uses <= 0:
            raise ValueError("max_results 与 max_uses 必须是正整数")

        unique_queries = list(dict.fromkeys(queries))
        if len(unique_queries) == 1:
            return self._search_one(unique_queries[0], max_results, max_uses)

        results: list[WebSearchResult | None] = [None] * len(unique_queries)
        first_error: Exception | None = None
        with ThreadPoolExecutor(max_workers=len(unique_queries)) as pool:
            pending: dict[Future[WebSearchResult], int] = {
                pool.submit(self._search_one, query, max_results, max_uses): index
                for index, query in enumerate(unique_queries)
            }
            for future in as_completed(pending):
                try:
                    results[pending[future]] = future.result()
                except Exception as error:  # noqa: BLE001 - 并发批次需归并任一查询失败
                    if first_error is None:
                        first_error = error
                    for sibling in pending:
                        sibling.cancel()

        if first_error is not None:
            raise first_error
        return self._merge_results(
            [cast(WebSearchResult, result) for result in results], max_results
        )

    def _search_one(
        self, query: str, max_results: int, max_uses: int
    ) -> WebSearchResult:
        """通过 DeepSeek provider 执行一条真实查询。"""
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
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Perform a web search for the query: {query}",
                            }
                        ],
                    }
                ],
                "tools": [
                    {
                        "type": WEB_SEARCH_TOOL,
                        "name": "web_search",
                        "max_uses": max_uses,
                    }
                ],
            },
            timeout=120,
        )
        if response.is_redirect:
            raise RuntimeError("[WEB_PROVIDER_ERROR] 搜索端点不允许 HTTP 重定向")
        response.raise_for_status()
        data = response.json()

        # provider 生成的 text 不是可信答案，只从 citations 取引用片段。
        snippets: dict[str, str] = {}
        for block in data.get("content", []):
            if block.get("type") != "text":
                continue
            for citation in block.get("citations") or []:
                url = citation.get("url")
                cited_text = citation.get("cited_text")
                if url and cited_text and url not in snippets:
                    snippets[url] = cited_text

        sources: list[WebSource] = []
        found_result_block = False
        for block in data.get("content", []):
            if block.get("type") == "web_search_tool_result":
                found_result_block = True
                for item in block.get("content", []):
                    if item.get("type") == "web_search_result" and item.get("url"):
                        url = item["url"]
                        sources.append(
                            WebSource(
                                title=item.get("title", ""),
                                url=url,
                                snippet=snippets.get(url),
                                published_at=item.get("page_age"),
                            )
                        )

        # 严格模式（官方行为）：没有搜索结果块 → 报错，绝不从文本里抓 URL
        if not found_result_block:
            raise RuntimeError(
                "[WEB_PROVIDER_ERROR] 响应中没有 web_search_tool_result 块"
            )
        # 按 URL 去重（官方「一次请求可能在多次搜索中呈现同一页面」）
        seen: set[str] = set()
        deduped: list[WebSource] = []
        for source in sources:
            if source.url in seen:
                continue
            seen.add(source.url)
            deduped.append(source)
        truncated = len(deduped) > max_results
        return WebSearchResult(
            sources=tuple(deduped[:max_results]), truncated=truncated
        )

    @staticmethod
    def _merge_results(
        results: list[WebSearchResult], max_results: int
    ) -> WebSearchResult:
        """按来源排名轮询合并，并跨查询按 URL 去重。"""
        merged: list[WebSource] = []
        seen: set[str] = set()
        dropped = False
        source_ranks = max((len(result.sources) for result in results), default=0)
        for rank in range(source_ranks):
            for result in results:
                if rank >= len(result.sources):
                    continue
                source = result.sources[rank]
                if source.url in seen:
                    continue
                seen.add(source.url)
                if len(merged) == max_results:
                    dropped = True
                    break
                merged.append(source)
            if dropped:
                break
        return WebSearchResult(
            sources=tuple(merged),
            truncated=dropped or any(result.truncated for result in results),
        )


def web_fetch(url: str, timeout_seconds: float = 20.0) -> str:
    """真实 HTTP GET 一个网页，提取标题与正文片段。

    教学版用最朴素的手段：httpx 获取 HTML，正则提 <title>，
    去标签后截取正文前 800 字符。真实产品会用可读性提取库。"""
    response = httpx.get(url, timeout=timeout_seconds, follow_redirects=True)
    response.raise_for_status()
    html = response.text
    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL
    )
    title = title_match.group(1).strip() if title_match else "(无标题)"
    # 去掉 script/style 与全部标签，留纯文本
    cleaned = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(r"<[^>]+>", " ", cleaned)
    text = re.sub(r"\s+", " ", text).strip()
    return f"标题: {title}\n\n正文片段: {text[:800]}"
