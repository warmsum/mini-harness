"""第 10 章 demo：文件工具的完整旅程。

运行（无需 API，纯本地）：
    uv run python chapters/10-filesystem/src/demo.py

旅程：读文件 → 未读就改（拒绝）→ 歧义编辑 → replace_all →
外部修改（mtime 变化）→ 写被拒 → 重读后写成功 → grep/glob →
逃出工作区（沙箱拒绝）→ 升级审批。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from fs_tools import ObservationTracker, edit_file, glob, grep, read_file, write_file
from sandbox import (
    DANGER_FULL_ACCESS,
    WORKSPACE_WRITE,
    SandboxDeniedError,
    SandboxPolicy,
    approve_escalation,
)


def section(title: str) -> None:
    print(f"\n━━━ {title} ━━━")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "workspace"
        ws.mkdir()
        (ws / "notes.txt").write_text("第一行：hello\n第二行：world\n", encoding="utf-8")
        (ws / "todo.txt").write_text("学习 sandbox\n学习 subagent\n", encoding="utf-8")
        policy = SandboxPolicy(mode=WORKSPACE_WRITE, workspace_root=ws)
        tracker = ObservationTracker()

        section("1. read_file：带行号 + 页脚")
        print(read_file(ws / "notes.txt", tracker))

        section("2. 观察策略：没读过的文件不许改")
        try:
            edit_file(ws / "todo.txt", "学习", "复习", policy, tracker)
        except PermissionError as e:
            print(f"  {e}")

        section("3. 歧义编辑：old_string 匹配多处")
        tracker.record_read(ws / "todo.txt")
        try:
            edit_file(ws / "todo.txt", "学习", "复习", policy, tracker)
        except ValueError as e:
            print(f"  {e}")
        print(
            "  "
            + edit_file(
                ws / "todo.txt", "学习", "复习", policy, tracker, replace_all=True
            )
        )

        section("4. 外部修改：读后写不是盲写（mtime CAS）")
        data = ws / "data.csv"
        data.write_text("name,score\n", encoding="utf-8")
        tracker.record_read(data)
        print(read_file(data, tracker))
        time.sleep(0.01)
        data.write_text("name,score\nexternal,edit\n", encoding="utf-8")  # 模拟外部修改
        try:
            write_file(data, "name,score\nmini,200\n", policy, tracker)
        except PermissionError as e:
            print(f"  {e}")
        print("  重新读取后：")
        print(read_file(data, tracker))
        print("  " + write_file(data, "name,score\nmini,200\n", policy, tracker))

        section("5. grep 与 glob")
        print(f"  grep 'sandbox':\n{grep(ws, 'sandbox')}")
        print(f"  glob '*.txt':\n{glob(ws, '*.txt')}")

        section("6. 逃出工作区：沙箱拒绝")
        try:
            # 目标在临时目录之外（家目录）——两个可写根都够不着
            write_file(Path.home() / ".mini-harness-escape-test.txt", "越界", policy, tracker)
        except SandboxDeniedError as e:
            print(f"  {e}")

        section("7. 升级审批：严格更宽")
        try:
            approve_escalation(policy, DANGER_FULL_ACCESS)
            print("  升级到 danger-full-access：获批（教学版直接放行）")
        except SandboxDeniedError as e:
            print(f"  {e}")


if __name__ == "__main__":
    main()
