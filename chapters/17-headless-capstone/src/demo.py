"""第 17 章 demo：组装后的 mini_harness 跑一个完整任务。

运行（在项目根目录，需要 .env）：
    uv run python chapters/17-headless-capstone/src/demo.py

演示：
1. 模块清单：前 16 章各贡献了哪一块；
2. 用组装的包跑真实任务（与 python -m mini_harness 等价）；
3. 会话落盘到临时目录并读回验证。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mini_harness.__main__ import run_task  # noqa: E402


def main() -> None:
    print("=== ① 组装清单：前 16 章各贡献了哪一块 ===")
    modules = [
        ("client.py", "第 01/02 章", "流式客户端 + 工具调用消息模型"),
        ("session.py", "第 05 章", "事件日志与消息投影"),
        ("registry.py / prompt.py", "第 06 章", "工具注册表 + 提示词组装"),
        ("agent.py / inbox.py", "第 07 章", "常驻循环与 inbox"),
        ("persistence.py", "第 08 章", "JSONL 持久化"),
        ("meter.py", "第 09 章", "token 计量"),
        ("calculator.py", "第 02 章", "计算器工具"),
    ]
    for file, chapter, what in modules:
        print(f"  {file:<28} 来自 {chapter:<12} {what}")

    print()
    print("=== ② 用组装的包跑真实任务 ===")
    with tempfile.TemporaryDirectory() as tmp:
        session_file = str(Path(tmp) / "session.jsonl")
        final_text, completed = run_task("1+2*3 等于几？", session_file=session_file)
        print(f"  [stdout] {final_text}")
        print(f"  [exit] {'0（正常完成）' if completed else '1（异常）'}")

        print()
        print("=== ③ 会话落盘并读回 ===")
        from mini_harness.persistence import JsonlStore

        loaded = JsonlStore(session_file).load()
        print(f"  读回 {len(loaded.events)} 条事件（第 08 章的持久化在工作）")


if __name__ == "__main__":
    main()
