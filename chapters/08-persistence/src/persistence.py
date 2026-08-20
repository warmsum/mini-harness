"""第 08 章：JSONL 持久化 —— 把会话日志写进磁盘。

对应官方 packages/session/session-persistence-jsonl。
教学版实现三个核心机制：
1. JSONL 格式：首行 header + 每行一条事件；
2. 原子发布：先写临时文件再 rename——崩溃时不会留下半截文件；
3. 崩溃修复：加载时截断残缺尾行，并为开放的工具、step、turn 合成收尾。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from session import Session, SessionEvent

HEADER_FORMAT = "mini-harness-jsonl"
HEADER_VERSION = 1
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"


class JsonlStore:
    """会话的 JSONL 落盘与加载。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------
    # 保存：首次原子发布，之后只追加
    # ------------------------------------------------------------------

    def save(self, session: Session) -> None:
        """首次用临时文件原子发布，之后只追加尚未落盘的事件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            lines = [_header_line(), *(_event_line(event) for event in session.events)]
            tmp_path = self.path.with_name(self.path.name + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as file:
                file.write("\n".join(lines) + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, self.path)
            return

        persisted, torn_offset = self._read_records()
        if torn_offset is not None:
            raise ValueError("会话文件存在残缺尾行；请先 load() 修复后再保存")
        current = session.events
        if len(persisted) > len(current) or tuple(persisted) != current[: len(persisted)]:
            raise ValueError("会话文件不是当前日志的前缀，拒绝覆盖既有历史")
        pending = current[len(persisted) :]
        if not pending:
            return
        with self.path.open("a", encoding="utf-8") as file:
            for event in pending:
                file.write(_event_line(event) + "\n")
            file.flush()
            os.fsync(file.fileno())

    # ------------------------------------------------------------------
    # 加载：校验 + 崩溃修复
    # ------------------------------------------------------------------

    def load(self) -> Session:
        """从磁盘重建会话。

        两个防护：
        1. header 校验：格式/版本不符立即失败——读到别人的文件
           或者未来版本的文件，响亮报错比静默解析安全；
        2. 崩溃修复：末尾残缺行（进程写到一半被杀）直接截断，
           并合成一条 turn/end 收尾——轮次边界在磁盘上也必须闭合。
        """
        if not self.path.exists():
            raise FileNotFoundError(f"会话文件不存在: {self.path}")

        events, torn_offset = self._read_records()
        if torn_offset is not None:
            with self.path.open("r+b") as file:
                file.truncate(torn_offset)
        # 崩溃可能恰好发生在完整行写完之后，因此即使没有 torn tail，
        # 也要根据已持久化事件检查开放状态。
        _append_recovery_closers(events)
        return Session.from_log(events)

    def _read_records(self) -> tuple[list[SessionEvent], int | None]:
        """读取完整行。只有最后一个未换行片段可以按崩溃尾部修复。"""
        raw_bytes = self.path.read_bytes()
        if not raw_bytes:
            raise ValueError("空的会话文件")
        complete_bytes = len(raw_bytes)
        torn_offset: int | None = None
        if not raw_bytes.endswith(b"\n"):
            last_newline = raw_bytes.rfind(b"\n")
            if last_newline < 0:
                raise ValueError("会话文件缺少完整 header")
            complete_bytes = last_newline + 1
            torn_offset = complete_bytes

        complete_lines = raw_bytes[:complete_bytes].decode("utf-8").splitlines()
        if not complete_lines:
            raise ValueError("会话文件缺少 header")
        try:
            header = json.loads(complete_lines[0])
        except json.JSONDecodeError as error:
            raise ValueError("会话文件 header 不是合法 JSON") from error
        if not isinstance(header, dict) or set(header) != {"format", "version"}:
            raise ValueError(f"无法识别的会话文件头: {header}")
        if header["format"] != HEADER_FORMAT or header["version"] != HEADER_VERSION:
            raise ValueError(f"无法识别的会话文件头: {header}")

        events: list[SessionEvent] = []
        for line_number, line in enumerate(complete_lines[1:], start=2):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"会话文件第 {line_number} 行损坏") from error
            if not isinstance(raw, dict) or set(raw) != {"id", "type", "ts", "data"}:
                raise ValueError(f"会话文件第 {line_number} 行不是合法事件")
            events.append(
                SessionEvent(id=raw["id"], type=raw["type"], ts=raw["ts"], data=raw["data"])
            )
        # 先走 Session 的连续 seq 与 lossless JSON 校验，再交还普通列表。
        return list(Session.from_log(events).events), torn_offset


def _header_line() -> str:
    return json.dumps({"format": HEADER_FORMAT, "version": HEADER_VERSION}, ensure_ascii=False)


def _event_line(event: SessionEvent) -> str:
    return json.dumps(
        {"id": event.id, "type": event.type, "ts": event.ts, "data": event.data},
        ensure_ascii=False,
        allow_nan=False,
    )


def _append_recovery_closers(events: list[SessionEvent]) -> None:
    """为开放的工具、step 与 turn 依次补上崩溃收尾。"""
    open_turn: int | None = None
    open_step: tuple[int, int] | None = None
    open_calls: dict[str, str] = {}
    for event in events:
        if event.type == "turn/start":
            open_turn = event.data["turn"]
        elif event.type == "turn/end":
            open_turn = None
        elif event.type == "step/start":
            open_step = (event.data["turn"], event.data["step"])
        elif event.type == "step/end":
            open_step = None
        elif event.type == "tool/call":
            open_calls[event.data["call_id"]] = event.data["name"]
        elif event.type == "tool/result":
            open_calls.pop(event.data["call_id"], None)

    timestamp = events[-1].ts if events else time.time()
    for call_id, name in open_calls.items():
        events.append(
            SessionEvent(
                id=len(events),
                type="tool/result",
                ts=timestamp,
                data={
                    "call_id": call_id,
                    "content": (
                        f"Error [{TOOL_OUTCOME_UNKNOWN}]: tool {name!r} was recorded, "
                        "but no result was durably recorded. Its outcome is unknown. "
                        "Retry only if the operation is read-only or idempotent; if it "
                        "may have side effects, first verify external state or ask the "
                        "user. Do not retry blindly."
                    ),
                    "is_error": True,
                },
            )
        )
    if open_step is not None:
        turn, step = open_step
        events.append(
            SessionEvent(
                id=len(events),
                type="step/end",
                ts=timestamp,
                data={"turn": turn, "step": step, "reason": "crashed"},
            )
        )
    if open_turn is not None:
        events.append(
            SessionEvent(
                id=len(events),
                type="turn/end",
                ts=timestamp,
                data={"turn": open_turn, "reason": "crashed"},
            )
        )
