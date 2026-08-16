"""第 01 章：最小流式 Agent。

这一章你亲手实现三样东西：
1. `load_api_key()`   —— 从项目根目录的 .env 读 API Key
2. `Message`          —— 一条不可变的对话消息
3. `DeepSeekClient`   —— 会「一次说完」(chat) 和「边想边说」(stream) 的模型客户端
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from httpx_sse import aconnect_sse

# ---------------------------------------------------------------------------
# 1. 从 .env 读 API Key
# ---------------------------------------------------------------------------


def load_api_key() -> str:
    """按「环境变量优先，其次 .env 文件」的顺序找 DeepSeek API Key。

    为什么要两步？部署到服务器时常用环境变量；本地学习时把 Key 写在 .env 更方便，
    而且 .env 已被 .gitignore 忽略，不会提交到 GitHub。
    """
    # 第一步：环境变量（例如终端里 export DEEPSEEK_API_KEY=...）
    from_env = os.getenv("DEEPSEEK_API_KEY")
    if from_env:
        return from_env

    # 第二步：项目根目录的 .env 文件。
    # 本文件位于 chapters/01-streaming-agent/src/，向上三级才是项目根目录。
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                # 去掉 "DEEPSEEK_API_KEY=" 前缀，再剥掉可能的引号
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value

    raise RuntimeError(
        "找不到 DEEPSEEK_API_KEY：请在项目根目录创建 .env，"
        "写入一行 DEEPSEEK_API_KEY=你的key（参考 .env.example）"
    )


# ---------------------------------------------------------------------------
# 2. 消息：对话历史里的最小单位
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """一条对话消息。

    `frozen=True` 表示创建后不能修改。为什么 Agent 的消息要不可变？
    因为对话历史会被反复读取（每一轮都要发给模型），任何一处代码悄悄改了
    历史内容，后面的行为就全都对不上了。先堵住这个口子，后面会反复受益。
    """

    role: str  # "system"（规则）/"user"（用户）/"assistant"（模型）
    content: str


# ---------------------------------------------------------------------------
# 3. 模型客户端：一次说完 vs 边想边说
# ---------------------------------------------------------------------------


class DeepSeekClient:
    """DeepSeek 的 OpenAI 兼容客户端，只用标准库 + httpx，零魔法。"""

    BASE_URL = "https://api.deepseek.com"
    MODEL = "deepseek-chat"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or load_api_key()

    # ---------- 3.1 非流式：一次拿回完整回答 ----------

    def chat(self, messages: list[Message]) -> str:
        """把整段对话发给模型，等它全部想完，一次性拿回完整回答。

        最简单、最适合起步的调用方式。缺点：长回答要等很久才看到第一个字。
        """
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.MODEL,
                    "messages": [{"role": m.role, "content": m.content} for m in messages],
                    "stream": False,  # 关键开关：False = 一次给全
                },
            )
            response.raise_for_status()  # 网络/认证出错时抛出带状态码的异常
            data = response.json()
            return data["choices"][0]["message"]["content"]

    # ---------- 3.2 流式：边生成边产出 ----------

    async def stream(self, messages: list[Message]):
        """流式调用：模型每想出一小段，就立即交出一小段（chunk）。

        这是一个「异步生成器」——调用方用 `async for` 遍历它，
        每迭代一次拿到一小段新文字，调用方立刻打印到终端。
        """
        async with httpx.AsyncClient(timeout=60) as client:
            # aconnect_sse 帮我们解析 SSE 协议。
            # SSE 是服务端持续推送数据的文本协议：每条数据以 "data: ..." 开头。
            async with aconnect_sse(
                client,
                "POST",
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.MODEL,
                    "messages": [{"role": m.role, "content": m.content} for m in messages],
                    "stream": True,  # 关键开关：True = 边生成边给
                },
            ) as event_source:
                async for event in event_source.aiter_sse():
                    if event.data == "[DONE]":
                        break  # DeepSeek 用这一行表示「全部说完了」
                    payload = json.loads(event.data)
                    delta = payload["choices"][0].get("delta", {})
                    piece = delta.get("content")
                    if piece:
                        yield piece  # 把这一小段文字交给调用方

    # ---------- 3.3 组装：把分片拼成一条完整消息 ----------

    async def stream_message(self, messages: list[Message]) -> Message:
        """流式调用的「正确收尾」：分片只在屏幕上显示，历史里只存完整消息。

        为什么需要这一步？如果每来一个字就往对话历史里塞一条，
        下次请求会带着几十条碎消息；中途断网时，历史里还会留下半句话。
        所以 Agent 的规矩是：分片实时展示没问题，但进历史的必须是一条
        完整、不可变的 Message。
        """
        pieces: list[str] = []
        async for piece in self.stream(messages):
            pieces.append(piece)
        return Message(role="assistant", content="".join(pieces))
