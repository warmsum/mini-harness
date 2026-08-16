"""第 11 章 demo：命令执行的边界与审批。

运行（无需 API，纯本地；审批用脚本化的「模拟用户」回答）：
    uv run python chapters/11-shell-sandbox/src/demo.py

四节：
① 只读命令放行（ls 成功，输出被捕获）
② read-only 模式拒绝写类命令（rm 被模式门挡住）
③ 审批流：ask + 模拟用户拒绝 / 批准；never 策略直接拒绝
④ 一次性授权：grant_once 后同一条命令放行，再用一次就失效
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from shell import (
    APPROVAL_ALLOWED_ONCE,
    APPROVAL_REJECTED,
    POLICY_NEVER,
    ShellPolicy,
)


def section(title: str) -> None:
    print(f"\n━━━ {title} ━━━")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        (workspace / "precious.txt").write_text("重要数据\n", encoding="utf-8")

        # 模拟用户：对任何命令都说「拒绝」（安全的默认）
        policy = ShellPolicy(mode="read-only", approver=lambda cmd: APPROVAL_REJECTED)

        section("① 只读命令：模式门放行")
        result = policy.execute("ls -1", cwd=str(workspace))
        print(f"  exit={result.exit_code}")
        print(f"  stdout:\n{result.stdout.strip()}")
        print(f"  stderr: {result.stderr.strip() or '(空)'}")

        section("② read-only 模式拒绝写类命令")
        result = policy.execute("rm -f precious.txt", cwd=str(workspace))
        print(f"  stderr: {result.stderr.strip()}")
        print(f"  文件还在吗: {(workspace / 'precious.txt').exists()}")

        section("③ 审批流：ask + 模拟用户（workspace-write 模式）")
        # 模拟用户拒绝
        refusing = ShellPolicy(
            mode="workspace-write", approver=lambda cmd: APPROVAL_REJECTED
        )
        denied = refusing.execute("rm -f precious.txt", cwd=str(workspace))
        print(f"  [用户拒绝] {denied.stderr.strip()}")
        # 换成「批准的模拟用户」
        permissive = ShellPolicy(
            mode="workspace-write", approver=lambda cmd: APPROVAL_ALLOWED_ONCE
        )
        allowed = permissive.execute("rm -f precious.txt", cwd=str(workspace))
        print(f"  [用户批准] exit={allowed.exit_code}，文件还在吗: {(workspace / 'precious.txt').exists()}")
        # never 策略：连审批都不问
        never = ShellPolicy(mode="workspace-write", approval_policy=POLICY_NEVER)
        result = never.execute("ls", cwd=str(workspace))
        print(f"  [never 策略] {result.stderr.strip()}")

        section("④ 一次性授权：grant_once 用后即焚")
        (workspace / "precious.txt").write_text("重要数据\n", encoding="utf-8")
        once = ShellPolicy(
            mode="read-only",  # 只读模式 + 写命令：本会被模式门拒绝
            approval_policy=POLICY_NEVER,  # 票据优先于模式门与审批
        )
        once.grant_once("rm -f precious.txt")
        first = once.execute("rm -f precious.txt", cwd=str(workspace))
        print(f"  第 1 次执行（持票据）: exit={first.exit_code}，文件还在吗: {(workspace / 'precious.txt').exists()}")
        (workspace / "precious.txt").write_text("重要数据\n", encoding="utf-8")
        second = once.execute("rm -f precious.txt", cwd=str(workspace))
        print(f"  第 2 次执行（票据已焚）: {second.stderr.strip()}")
        print(f"  文件还在吗: {(workspace / 'precious.txt').exists()}")


if __name__ == "__main__":
    main()
