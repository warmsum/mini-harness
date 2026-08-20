"""第 08 章 demo：落盘 → 读回 → 崩溃修复。

运行（无需 API，纯本地，确定性输出）：
    uv run python chapters/08-persistence/src/demo.py

四节：
① 手造一段会话 → save → 打印磁盘上的 JSONL 原文
② load → 重放一致性校验
③ checkpoint 在工具副作用前持久化 call
④ 模拟崩溃（往文件尾追加半行）→ load → 观察合成 turn/end
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from checkpoint import CheckpointPolicy
from persistence import JsonlStore
from session import Session


def build_session() -> Session:
    """手造一段会话（本章只教持久化，不调用模型）。"""
    session = Session()
    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": "1+2*3 等于几？"})
    session.append(
        "assistant/message",
        {"content": None, "tool_calls": [{"id": "call_1", "name": "calculator", "arguments": '{"expression": "1+2*3"}'}]},
    )
    session.append("tool/result", {"call_id": "call_1", "content": "7.0", "is_error": False})
    session.append("assistant/message", {"content": "1+2*3 = 7"})
    session.append("turn/end", {"turn": 1, "reason": "completed"})
    return session


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "session.jsonl"
        store = JsonlStore(path)

        print("=== ① 落盘：磁盘上的 JSONL 原文 ===")
        store.save(build_session())
        for line in path.read_text(encoding="utf-8").splitlines():
            print(f"  {line[:100]}{'…' if len(line) > 100 else ''}")
        print("  ← 首行 header，之后每行一条事件")

        print()
        print("=== ② 读回：重放一致性 ===")
        loaded = store.load()
        original = build_session()
        same = [e.type for e in loaded.events] == [e.type for e in original.events]
        print(f"  读回 {len(loaded.events)} 条事件，类型序列与原始一致: {same}")

        print()
        print("=== ③ checkpoint：工具意图先落盘，副作用后执行 ===")
        checkpoint_path = Path(tmp) / "checkpoint.jsonl"
        checkpoint_store = JsonlStore(checkpoint_path)
        checkpoint = CheckpointPolicy(checkpoint_store.save)
        pending = Session()
        pending.append("turn/start", {"turn": 1})
        pending.append("step/start", {"turn": 1, "step": 1})
        pending.append(
            "tool/call",
            {"call_id": "call-side-effect", "name": "write", "arguments": "{}"},
        )
        checkpoint.before_tool(pending)
        persisted = JsonlStore(checkpoint_path).load()
        print(f"  副作用前磁盘末事件: {persisted.events[2].type}")
        print("  ← flush 失败时，调用方不会进入工具正文")

        print()
        print("=== ④ 模拟崩溃：最后一条 turn/end 还没写，进程就被杀 ===")
        # 1) 把文件的最后一行（turn/end）删掉——模拟「轮次还没收尾就崩溃」
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        # 2) 再追加半行——模拟「正在写一条事件时被杀」
        with path.open("a", encoding="utf-8") as f:
            f.write('{"id": 5, "type": "assistant/message", "data": {"con')
        recovered = store.load()
        for event in recovered.events:
            note = "  ← 合成收尾" if event.data.get("reason") == "crashed" else ""
            print(f"  #{event.id:<2} {event.type}{note}")
        print("  ← 残缺尾行被截断，缺失的轮次收尾被合成 turn/end 补上")


if __name__ == "__main__":
    main()
