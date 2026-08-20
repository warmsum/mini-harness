"""第 13 章复用的消息与工具数据结构。

本章只演示本地状态和交互接缝，不调用模型，因此不携带 HTTP 客户端。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolCall:
    """模型发起的一次工具调用请求。"""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Message:
    """会话投影得到的一条模型消息。"""

    role: str
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class Tool:
    """模型可见的工具声明与本地执行函数。"""

    name: str
    description: str
    parameters: dict[str, Any]
    execute: Callable[[dict[str, Any]], str]
