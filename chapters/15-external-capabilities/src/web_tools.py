"""第 15 章：真实的外部能力 —— Web Search 与网页抓取。

对应官方 packages/web/web-search-deepseek 与 tool-web。
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
from dataclasses import dataclass

import httpx

# 官方默认值（web-search-deepseek 配置表）
SEARCH_BASE_URL = "https://api.deepseek.com/anthropic/v1"
SEARCH_MODEL = "deepseek-v4-flash"
ANTHROPIC_VERSION = "2023-06-01"
WEB_SEARCH_TOOL = "web_search_20250305"


@dataclass(frozen=True)
class WebSource:
    """一条搜索来源。"""

    title: str
    url: str


@dataclass(frozen=True)
class WebSearchResult:
    """一次搜索的结构化结果（对应官方 WebSearchResult 的简化版）。"""

    sources: tuple[WebSource, ...]
    answer: str  # 模型基于搜索结果生成的回答
    truncated: bool = False


class WebSearchClient:
    """DeepSeek Web Search 客户端。

    与第 01 章的 chat 是两套协议：这里走 Anthropic 兼容的
    /messages 端点（不是 chat/completions），密钥复用同一个
    DEEPSEEK_API_KEY（官方明确「不增加密钥」）。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = SEARCH_BASE_URL,
        model: str = SEARCH_MODEL,
    ) -> None:
        if api_key is None:
            from client import load_api_key

            api_key = load_api_key()
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def search(self, query: str, max_uses: int = 3) -> WebSearchResult:
        """执行一次真实搜索：服务器侧搜索，返回结构化结果。"""
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
                    {
                        "type": WEB_SEARCH_TOOL,
                        "name": "web_search",
                        "max_uses": max_uses,
                    }
                ],
            },
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()

        # 解析结构化来源（官方映射：url/title 一一对应；
        # 官方还映射 page_age → publishedAt，教学版略）
        sources: list[WebSource] = []
        answer = ""
        for block in data.get("content", []):
            if block.get("type") == "web_search_tool_result":
                for item in block.get("content", []):
                    if item.get("type") == "web_search_result":
                        sources.append(
                            WebSource(title=item.get("title", ""), url=item.get("url", ""))
                        )
            elif block.get("type") == "text":
                answer += block.get("text", "")

        # 严格模式（官方行为）：没有搜索结果块 → 报错，绝不从文本里抓 URL
        if not sources:
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
        return WebSearchResult(sources=tuple(deduped), answer=answer.strip())


def web_fetch(url: str, timeout_seconds: float = 20.0) -> str:
    """真实 HTTP GET 一个网页，提取标题与正文片段。

    教学版用最朴素的手段：requests 拿 HTML，正则提 <title>，
    去标签后截取正文前 800 字符。真实产品会用可读性提取库。"""
    response = httpx.get(url, timeout=timeout_seconds, follow_redirects=True)
    response.raise_for_status()
    html = response.text
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else "(无标题)"
    # 去掉 script/style 与全部标签，留纯文本
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", cleaned)
    text = re.sub(r"\s+", " ", text).strip()
    return f"标题: {title}\n\n正文片段: {text[:800]}"
