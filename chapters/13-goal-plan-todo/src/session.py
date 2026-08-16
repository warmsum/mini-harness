"""第 05 章：事件日志 —— 会话的「唯一事实来源」。

对应官方 packages/core/session（事件溯源的会话日志）。
核心思想：
1. 日志是 append-only（只追加）：事件一旦写入永不修改；
2. 消息历史是「派生视图」：derive_messages() 每次从日志投影；
3. 日志服务所有消费者：模型请求、持久化、界面展示、重放都读同一份。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from client import Message, ToolCall


@dataclass(frozen=True)
class SessionEvent:
    """日志里的一条事件。

    - id：从 0 开始连续递增（重放校验靠它）；
    - type：事件类型（turn/start、user/message、tool/result……）；
    - data：事件内容，写入时深冻结，之后不可修改。
    """

    id: int
    type: str
    ts: float
    data: dict[str, Any]


class Session:
    """一次对话的全部历史：一条只追加的事件日志。"""

    def __init__(self) -> None:
        self._log: list[SessionEvent] = []
        self._snapshot: list[SessionEvent] | None = None
        self._listeners: list[Any] = []

    # ------------------------------------------------------------------
    # 追加：校验 + 冻结 + 通知
    # ------------------------------------------------------------------

    def append(self, type: str, data: dict[str, Any]) -> SessionEvent:
        """追加一条事件。三个动作：

        1. 校验 data 是可序列化的纯 JSON（拒绝函数、集合等）；
        2. 深冻结 data——日志是不可变历史，防任何后续篡改；
        3. 缓存快照失效，通知订阅者（持久化插件的接缝，第 08 章兑现）。
        """
        frozen_data = _freeze_json(data)
        event = SessionEvent(id=len(self._log), type=type, ts=_now(), data=frozen_data)
        self._log.append(event)
        self._snapshot = None
        for listener in list(self._listeners):
            listener(event)
        return event

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    @property
    def events(self) -> list[SessionEvent]:
        """冻结快照：append 后重建（缓存避免每次全量复制）。"""
        if self._snapshot is None:
            self._snapshot = list(self._log)
        return self._snapshot

    @property
    def seq(self) -> int:
        """下一条事件的 id，恒等于日志长度。"""
        return len(self._log)

    def subscribe(self, listener: Any) -> Any:
        """订阅新事件（返回解绑函数）。持久化插件挂在这里。"""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            self._listeners.remove(listener)

        return unsubscribe

    # ------------------------------------------------------------------
    # 投影：日志 → 模型看到的消息历史
    # ------------------------------------------------------------------

    def derive_messages(self) -> list[Message]:
        """把日志投影成 LLM 消息历史。

        只有三种事件会投影成消息（对应官方 surface 层）：
          user/message      → role="user"
          assistant/message → role="assistant"（含 tool_calls）
          tool/result       → role="tool"（带 tool_call_id）
        其余事件（turn/start、tool/call、turn/end……）只记日志，不发给模型。
        """
        messages: list[Message] = []
        for event in self._log:
            message = _derive_event_message(event)
            if message is not None:
                messages.append(message)
        return messages

    # ------------------------------------------------------------------
    # 重放：从既有日志重建会话
    # ------------------------------------------------------------------

    @classmethod
    def from_log(cls, events: list[SessionEvent]) -> "Session":
        """从既有日志重建会话（恢复/重放的入口）。

        校验 id 从 0 连续——日志是事实来源，缺一条都要响亮失败，
        而不是带着残缺历史继续跑。
        """
        session = cls()
        for index, event in enumerate(events):
            if event.id != index:
                raise ValueError(
                    f"重放失败：第 {index} 个事件 id 为 {event.id}（应为 {index}）"
                )
            session._log.append(event)
        return session


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _freeze_json(value: Any, _path: set[int] | None = None) -> Any:
    """深冻结 + 纯 JSON 校验。拒绝不可序列化的类型与循环引用。"""
    path = _path if _path is not None else set()
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, path) for item in value)
    if isinstance(value, dict):
        if id(value) in path:
            raise ValueError("事件 data 不能包含循环引用")
        path.add(id(value))
        result = {
            key: _freeze_json(item, path) for key, item in value.items()
        }
        path.discard(id(value))
        return result
    raise ValueError(f"事件 data 不能包含 {type(value).__name__}（仅限纯 JSON）")


def _derive_event_message(event: SessionEvent) -> Message | None:
    """单条事件的投影规则（对应官方 deriveEventMessage）。"""
    if event.type == "user/message":
        return Message(role="user", content=event.data["content"])
    if event.type == "assistant/message":
        raw_calls = event.data.get("tool_calls") or []
        tool_calls = tuple(
            ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
            for c in raw_calls
        )
        # 空内容且无工具调用的事件不投影（它只是日志记录）
        if event.data.get("content") is None and not tool_calls:
            return None
        return Message(
            role="assistant",
            content=event.data.get("content"),
            tool_calls=tool_calls,
        )
    if event.type == "tool/result":
        return Message(
            role="tool",
            content=event.data["content"],
            tool_call_id=event.data["call_id"],
        )
    return None
