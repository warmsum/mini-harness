"""第 02 章：支持工具调用的客户端与消息模型。

在第 01 章的 DeepSeekClient 基础上扩展三样东西：
1. `ToolCall` —— 模型发起的一次工具调用请求
2. `Message` 扩展 —— assistant 消息可携带 tool_calls；工具结果以 role="tool" 回灌
3. `DeepSeekClient.chat()` 支持传入工具清单、解析模型返回的 tool_calls
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
from httpx_sse import aconnect_sse

# ---------------------------------------------------------------------------
# 1. 从 .env 读 API Key（与第 01 章相同）
# ---------------------------------------------------------------------------


def load_api_key() -> str:
    """按「环境变量优先，其次 .env 文件」的顺序找 DeepSeek API Key。"""
    from_env = os.getenv("DEEPSEEK_API_KEY")
    if from_env:
        return from_env
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    raise RuntimeError("找不到 DEEPSEEK_API_KEY：请参考 .env.example 创建 .env")


# ---------------------------------------------------------------------------
# 2. 消息模型：新增工具调用的三个概念
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """模型发起的一次工具调用请求。

    arguments 是 JSON 字符串而不是 Python 对象——这是 OpenAI 兼容协议的
    规定：模型生成的参数是文本，必须先 json.loads 解析才能执行。
    """

    id: str       # 调用编号，工具结果回灌时靠它一一对应
    name: str     # 要调用的工具名
    arguments: str  # 参数 JSON 字符串，例如 '{"expression": "1+2*3"}'


@dataclass(frozen=True)
class Message:
    """一条对话消息。相对第 01 章多了两个可选字段：

    - tool_calls：assistant 消息可以携带一组工具调用请求；
    - tool_call_id：role="tool" 的消息用它标明「这是对哪次调用的回答」。
    """

    role: str
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class Tool:
    """一个 Agent 可用的工具。

    - name/description/parameters 是「给模型看的说明书」——模型读它们来决定
      什么时候调用、传什么参数；
    - execute 是「给程序跑的代码」——真正的计算在 Agent 进程里完成。
    """

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema，描述参数长什么样
    execute: Callable[[dict[str, Any]], str]


# ---------------------------------------------------------------------------
# 3. 客户端：请求带工具清单，响应解析 tool_calls
# ---------------------------------------------------------------------------


class DeepSeekClient:
    BASE_URL = "https://api.deepseek.com"
    MODEL = "deepseek-chat"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or load_api_key()

    @staticmethod
    def _wire_message(m: Message) -> dict[str, Any]:
        """把内部 Message 转成协议要求的 dict。只有 role="tool" 的消息结构特殊：
        它必须带 tool_call_id，让服务器知道这条结果是回答哪次调用的。"""
        wire: dict[str, Any] = {"role": m.role}
        if m.content is not None:
            wire["content"] = m.content
        if m.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in m.tool_calls
            ]
        if m.tool_call_id is not None:
            wire["tool_call_id"] = m.tool_call_id
        return wire

    def chat(self, messages: list[Message], tools: list[Tool] | None = None) -> Message:
        """非流式调用。本章的 Agent 循环用它：一次拿回完整回复（含 tool_calls），
        逻辑最清晰。流式工具分片的组装留到练习与官方对照。"""
        payload: dict[str, Any] = {
            "model": self.MODEL,
            "messages": [self._wire_message(m) for m in messages],
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        choice = data["choices"][0]
        raw_message = choice["message"]
        tool_calls: list[ToolCall] = []
        for raw_call in raw_message.get("tool_calls") or []:
            tool_calls.append(
                ToolCall(
                    id=raw_call["id"],
                    name=raw_call["function"]["name"],
                    arguments=raw_call["function"]["arguments"],
                )
            )
        return Message(
            role="assistant",
            content=raw_message.get("content"),
            tool_calls=tuple(tool_calls),
        )

    # ---------- 流式方法（与第 01 章相同，用于终端展示） ----------

    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """流式调用：只处理纯文本回答的展示。工具调用场景见 README 对照。"""
        completed = False
        async with httpx.AsyncClient(timeout=60) as client:
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
                    "messages": [self._wire_message(m) for m in messages],
                    "stream": True,
                },
            ) as event_source:
                async for event in event_source.aiter_sse():
                    if event.data == "[DONE]":
                        completed = True
                        break
                    payload = json.loads(event.data)
                    delta = payload["choices"][0].get("delta", {})
                    piece = delta.get("content")
                    if piece:
                        yield piece
        if not completed:
            raise RuntimeError("流式响应在 [DONE] 之前中断，拒绝保存不完整消息")
