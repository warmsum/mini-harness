"""事件日志：会话的「唯一事实来源」（第 05 章首次实现）。

对应官方 packages/core/session（事件溯源的会话日志）。
核心思想：
1. 日志是 append-only（只追加）：事件一旦写入永不修改；
2. 消息历史是「派生视图」：derive_messages() 每次从日志投影；
3. 日志服务所有消费者：模型请求、持久化、界面展示、重放都读同一份。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import copysign, isfinite
from typing import Any, Never

from .client import Message, ToolCall


class FrozenDict(dict[str, Any]):
    """JSON 可序列化、但写入后不可修改的字典。"""

    @staticmethod
    def _immutable() -> Never:
        raise TypeError("FrozenDict 不可修改")

    def __setitem__(self, key: str, value: Any) -> None:
        self._immutable()

    def __delitem__(self, key: str) -> None:
        self._immutable()

    def __ior__(self, value: Any) -> Never:  # type: ignore[misc]
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def pop(self, key: str, default: Any = None) -> Any:
        self._immutable()

    def popitem(self) -> Never:
        self._immutable()

    def setdefault(self, key: str, default: Any = None) -> Any:
        self._immutable()

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._immutable()

    @classmethod
    def build(cls, items: dict[str, Any]) -> FrozenDict:
        frozen = cls()
        for key, value in items.items():
            dict.__setitem__(frozen, key, value)
        return frozen


@dataclass(frozen=True)
class SessionEvent:
    """日志里的一条事件。

    - id：从 0 开始连续递增（重放校验靠它）；
    - type：事件类型（turn/start、user/message、tool/result……）；
    - data：事件内容，写入时冻结，之后不可修改。
    """

    id: int
    type: str
    ts: float
    data: Mapping[str, Any]


class Session:
    """一次对话的全部历史：一条只追加的事件日志。"""

    def __init__(self) -> None:
        self._log: list[SessionEvent] = []
        self._snapshot: tuple[SessionEvent, ...] | None = None
        self._listeners: list[Any] = []

    # ------------------------------------------------------------------
    # 追加：校验 + 冻结 + 通知
    # ------------------------------------------------------------------

    def append(self, type: str, data: dict[str, Any]) -> SessionEvent:
        """追加一条事件。三个动作：

        1. 校验 data 是可序列化的纯 JSON（拒绝函数、集合等）；
        2. 冻结 data——日志是不可变历史，防任何后续篡改；
        3. 缓存快照失效，通知订阅者（持久化等消费者的接缝）。
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
    def events(self) -> tuple[SessionEvent, ...]:
        """冻结快照：append 后重建（缓存避免每次全量复制）。"""
        if self._snapshot is None:
            self._snapshot = tuple(self._log)
        return self._snapshot

    @property
    def seq(self) -> int:
        """下一条事件的 id，恒等于日志长度。"""
        return len(self._log)

    def subscribe(self, listener: Any) -> Any:
        """订阅新事件（返回解绑函数）。持久化插件挂在这里。"""
        self._listeners.append(listener)
        active = True

        def unsubscribe() -> None:
            nonlocal active
            if not active:
                return
            active = False
            self._listeners.remove(listener)

        return unsubscribe

    # ------------------------------------------------------------------
    # 投影：日志 → 模型看到的消息历史
    # ------------------------------------------------------------------

    def derive_messages(self) -> list[Message]:
        """把日志投影成 LLM 消息历史。

        只有三种事件会投影成消息（对应官方 surface 层）：
          user/message      → role="user"
          assistant/message → role="assistant"（含 reasoning_content、tool_calls）
          tool/result       → role="tool"（带 tool_call_id）
        其余事件（turn/start、tool/call、turn/end……）只记日志，不发给模型。
        """
        messages: list[Message] = []
        for event in _surface_events(self._log):
            message = _derive_event_message(event)
            if message is not None:
                messages.append(message)
        return messages

    # ------------------------------------------------------------------
    # 重放：从既有日志重建会话
    # ------------------------------------------------------------------

    @classmethod
    def from_log(cls, events: Iterable[SessionEvent]) -> Session:
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
            if not isinstance(event.type, str) or not event.type:
                raise ValueError(f"重放失败：第 {index} 个事件 type 无效")
            if not isinstance(event.ts, (int, float)) or not isfinite(event.ts):
                raise ValueError(f"重放失败：第 {index} 个事件 ts 无效")
            frozen_data = _freeze_json(event.data)
            if not isinstance(frozen_data, FrozenDict):
                raise TypeError(f"重放失败：第 {index} 个事件 data 必须是对象")
            session._log.append(
                SessionEvent(
                    id=event.id,
                    type=event.type,
                    ts=float(event.ts),
                    data=frozen_data,
                )
            )
        return session


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _freeze_json(value: Any, _path: set[int] | None = None) -> Any:
    """冻结 + lossless JSON 校验。拒绝异常数字、非字符串键和循环引用。"""
    path = _path if _path is not None else set()
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise ValueError("事件 data 的整数超出 JSON 安全范围")
        return value
    if isinstance(value, float):
        if not isfinite(value) or (value == 0.0 and copysign(1.0, value) < 0):
            raise ValueError("事件 data 不能包含非有限数或负零")
        return value
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in path:
            raise ValueError("事件 data 不能包含循环引用")
        path.add(marker)
        try:
            return tuple(_freeze_json(item, path) for item in value)
        finally:
            path.remove(marker)
    if isinstance(value, dict):
        marker = id(value)
        if marker in path:
            raise ValueError("事件 data 不能包含循环引用")
        path.add(marker)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("事件 data 的对象键必须是字符串")
                result[key] = _freeze_json(item, path)
            return FrozenDict.build(result)
        finally:
            path.remove(marker)
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
        reasoning_content = event.data.get("reasoning_content")
        # 文本、思考与工具调用都没有的事件不投影（它只是日志记录）
        if (
            event.data.get("content") is None
            and not reasoning_content
            and not tool_calls
        ):
            return None
        return Message(
            role="assistant",
            content=event.data.get("content"),
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
        )
    if event.type == "tool/result":
        return Message(
            role="tool",
            content=event.data["content"],
            tool_call_id=event.data["call_id"],
        )
    return None


def _surface_events(events: list[SessionEvent]) -> list[SessionEvent]:
    """应用 append-only replacement，模型只看当前表层节点。"""
    nodes: list[SessionEvent] = []
    for event in events:
        if event.type not in {"user/message", "assistant/message", "tool/result"}:
            continue
        operation = event.data.get("surface_op")
        if not isinstance(operation, dict) or operation.get("op") != "replace":
            nodes.append(event)
            continue
        start = operation.get("start")
        end = operation.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or start > end:
            raise ValueError("surface replacement 范围无效")
        indexes = [index for index, node in enumerate(nodes) if start <= node.id <= end]
        if not indexes:
            raise ValueError("surface replacement 引用了不存在的节点")
        nodes[indexes[0] : indexes[-1] + 1] = [event]
    return nodes
