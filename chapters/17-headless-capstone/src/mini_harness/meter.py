"""第 09 章：Token 计量 —— 感知上下文压力。

对应官方 packages/llm/token-meter。官方用一个固定启发式估算 token：
每 token 按 4 个字符计，外加角色与结构开销，
不引入真实 tokenizer。教学版复刻同一思路。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import Message

# 官方启发式：4 字符 ≈ 1 token
CHARS_PER_TOKEN = 4
# 每条消息的角色/结构开销（官方 estimateMessage = 内容估算 + 4）
ROLE_OVERHEAD = 4
# DeepSeek 官方适配器默认上下文容量：1e6 token
DEFAULT_CONTEXT_WINDOW = 1_000_000
# 压力阈值：占用 >= 80% 触发（对齐官方压缩包 DEFAULT_THRESHOLD_RATIO=.8）
PRESSURE_THRESHOLD = 0.8


@dataclass(frozen=True)
class Measurement:
    """一次计量快照。"""

    messages: list[dict[str, Any]]
    message_tokens: int
    tools_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class Pressure:
    """压力换算：总量、容量、占比、是否越线。"""

    total_tokens: int
    context_window: int
    ratio: float
    over_threshold: bool


def estimate_tokens(text: str) -> int:
    """纯函数：一段文本的估算 = ceil(字符数 / 4)。"""
    return -(-len(text) // CHARS_PER_TOKEN)  # 向上取整的整除写法


def estimate_message(message: Message) -> int:
    """一条消息的估算 = 内容 + 角色开销。"""
    content = message.content or ""
    return estimate_tokens(content) + ROLE_OVERHEAD


def estimate_tools(tools: list[Any]) -> int:
    """工具 schema 的结构开销：序列化后按启发式估算。
    工具清单每次请求都要随 system 一起发送，是实打实的输入成本。"""
    import json

    if not tools:
        return 0
    return estimate_tokens(json.dumps(tools, ensure_ascii=False)) + ROLE_OVERHEAD


class TokenMeter:
    """计量服务。对应官方单例 ctx.tokenMeter。"""

    def __init__(self, context_window: int = DEFAULT_CONTEXT_WINDOW) -> None:
        self.context_window = context_window

    def measure(self, messages: list[Message], tools: list[Any] | None = None) -> Measurement:
        """计量一段会话：消息面 + 工具的 envelope。"""
        metered = [
            {
                "role": m.role,
                "length": len(m.content or ""),
                "tokens": estimate_message(m),
            }
            for m in messages
        ]
        message_tokens = sum(estimate_message(message) for message in messages)
        tools_tokens = estimate_tools(tools or [])
        return Measurement(
            messages=metered,
            message_tokens=message_tokens,
            tools_tokens=tools_tokens,
            total_tokens=message_tokens + tools_tokens,
        )

    def pressure(self, measurement: Measurement) -> Pressure:
        """把计量换算成压力读数。判定（是否压缩）留给消费方——
        计量与压缩解耦是官方明确的设计（token-meter 不依赖压缩包）。"""
        ratio = measurement.total_tokens / self.context_window
        return Pressure(
            total_tokens=measurement.total_tokens,
            context_window=self.context_window,
            ratio=ratio,
            over_threshold=ratio >= PRESSURE_THRESHOLD,
        )
