"""第 15 章 demo：真实的外部能力。

运行（在项目根目录，需要 .env）：
    uv run python chapters/15-external-capabilities/src/demo.py

两节（都是真实网络调用）：
① Web Search：真实调用 DeepSeek Anthropic 兼容端点，服务器侧搜索，
   打印结构化来源与模型回答；
② web_fetch：真实 HTTP GET DeepSeek Harness 的 GitHub 页面，
   提取标题与正文片段。
"""

from __future__ import annotations

from web_tools import WebSearchClient, web_fetch


def main() -> None:
    print("=== ① Web Search：真实搜索 DeepSeek Harness ===")
    client = WebSearchClient()
    result = client.search("DeepSeek Harness 是什么？官方仓库地址")
    print(f"  来源（{len(result.sources)} 条）：")
    for source in result.sources:
        print(f"  - {source.title}")
        print(f"    {source.url}")
    print()
    print("  模型基于搜索结果的回答（节选）：")
    for line in result.answer.splitlines()[:8]:
        print(f"  {line}")
    print("  …")

    print()
    print("=== ② web_fetch：真实抓取网页 ===")
    content = web_fetch("https://github.com/deepseek-ai/DeepSeek-Harness")
    for line in content.splitlines():
        print(f"  {line[:100]}")
    print("  …（正文片段截断于 800 字符）")


if __name__ == "__main__":
    main()
