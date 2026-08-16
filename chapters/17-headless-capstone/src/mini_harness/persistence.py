"""第 08 章：JSONL 持久化 —— 把会话日志写进磁盘。

对应官方 packages/session/session-persistence-jsonl。
教学版实现三个核心机制：
1. JSONL 格式：首行 header + 每行一条事件；
2. 原子发布：先写临时文件再 rename——崩溃时不会留下半截文件；
3. 崩溃修复：加载时截断残缺尾行，合成 turn/end 收尾。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .session import Session, SessionEvent

HEADER_FORMAT = "mini-harness-jsonl"
HEADER_VERSION = 1


class JsonlStore:
    """会话的 JSONL 落盘与加载。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------
    # 保存：原子发布
    # ------------------------------------------------------------------

    def save(self, session: Session) -> None:
        """把会话全部事件写成 JSONL 文件。

        原子发布的关键：先写临时文件，写完再 os.replace 换名。
        os.replace 在同一文件系统内是原子操作——任何时刻打开目标
        路径，看到的要么是完整的旧文件、要么是完整的新文件，
        绝不存在写了一半的状态。（官方 :43 用硬链接发布 + fsync
        达成同样效果，教学版用 os.replace 的等价语义。）
        """
        lines = [
            json.dumps({"format": HEADER_FORMAT, "version": HEADER_VERSION}, ensure_ascii=False)
        ]
        for event in session.events:
            lines.append(
                json.dumps(
                    {
                        "id": event.id,
                        "type": event.type,
                        "ts": event.ts,
                        "data": event.data,
                    },
                    ensure_ascii=False,
                )
            )
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp_path, self.path)

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

        lines = self.path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        if (
            header.get("format") != HEADER_FORMAT
            or header.get("version") != HEADER_VERSION
        ):
            raise ValueError(f"无法识别的会话文件头: {header}")

        events: list[SessionEvent] = []
        for line in lines[1:]:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                # 残缺尾行：截断 + 合成收尾，之后的行全部忽略
                events.append(
                    SessionEvent(
                        id=len(events),
                        type="turn/end",
                        ts=0.0,
                        data={"turn": 0, "reason": "crashed"},
                    )
                )
                break
            events.append(
                SessionEvent(
                    id=raw["id"], type=raw["type"], ts=raw["ts"], data=raw["data"]
                )
            )
        return Session.from_log(events)
