"""第 11 章：命令执行与审批 —— 给 Agent 最危险的一只手。

对应官方 packages/shell（执行器 seam）+ packages/interaction/user-approval。
教学版实现三件事：
1. run_command —— subprocess 执行 + 超时 + 输出捕获；
2. ApprovalPolicy —— 审批策略：ask（询问）/ never（直接拒绝）；
3. approve_once —— 一次性授权：allowed-once 只放行所请求的那一个动作。

诚实边界：官方 bash-sandbox 用内核级隔离（seatbelt/landlock）把
「文件写效应」挡在系统调用层，并明确限制只覆盖文件影响；
教学版不实现内核沙箱，只实现「模式门
+ 审批」的决策层，并在对照表中明确差异。
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

# 审批结果四值（对应官方 dsh-user-approval 的四结果枚举）：
# allowed-once 是唯一放行值——一次性授权，只作用于所请求的那一个动作
APPROVAL_ALLOWED_ONCE = "allowed-once"
APPROVAL_REJECTED = "rejected"
APPROVAL_CANCELLED = "cancelled"
APPROVAL_UNAVAILABLE = "unavailable"

# 审批策略：ask = 走审批通道；never = 直接拒绝（官方 ApprovalPolicy）
POLICY_ASK = "ask"
POLICY_NEVER = "never"

# 只读命令白名单：read-only 模式下仅这些前缀的命令放行。
# （教学简化——真实内核沙箱按系统调用拦截，不看命令文本。）
READ_ONLY_COMMANDS = {"ls", "cat", "head", "tail", "grep", "pwd", "wc"}


@dataclass(frozen=True)
class CommandResult:
    """一次命令执行的完整结果。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_command(
    command: str,
    cwd: str,
    timeout_seconds: float = 30.0,
    *,
    use_shell: bool = True,
) -> CommandResult:
    """执行一条 shell 命令，捕获输出，强制超时。

    subprocess 三件套：
    - capture_output：stdout/stderr 不刷屏，收进结果里；
    - timeout：命令挂死（如 sleep 9999）时强行杀掉——Agent 的
      命令绝不能无限期占住进程；
    - shell=True：按 shell 语法解析（管道、重定向都可用）。
    """
    try:
        argv: str | list[str] = command if use_shell else shlex.split(command)
        completed = subprocess.run(
            argv,
            shell=use_shell,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return CommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except subprocess.TimeoutExpired as error:
        return CommandResult(
            exit_code=-1,
            stdout=(error.stdout or b"").decode() if isinstance(error.stdout, bytes) else "",
            stderr=f"命令超时（>{timeout_seconds}s），已强制终止",
            timed_out=True,
        )


class ShellPolicy:
    """命令执行的决策层：模式门 + 审批。

    决策顺序（对应官方 sandbox 决策的简化版）：
    1. 模式门：read-only 只放行白名单只读命令；更宽模式放行一切；
    2. 审批：policy=never 直接拒绝；policy=ask 调用审批回调；
    3. allowed-once：一次性授权，用一次即失效。
    """

    def __init__(
        self,
        mode: str = "read-only",
        approval_policy: str = POLICY_ASK,
        approver: Callable[[str], str] | None = None,
    ) -> None:
        self.mode = mode
        self.approval_policy = approval_policy
        # 审批回调：返回 APPROVAL_ALLOWED_ONCE / APPROVAL_REJECTED / ...
        self.approver = approver or (lambda command: APPROVAL_REJECTED)
        # 一次性授权票据：非 None 时本次命令免审，用后即焚
        self._granted_once: str | None = None

    def grant_once(self, command: str) -> None:
        """签发一次性授权（对应官方 allowed-once：只作用于所请求的那一个动作）。"""
        self._granted_once = command

    def decide(self, command: str) -> tuple[bool, str]:
        """决定一条命令能否执行。返回 (放行?, 理由)。

        决策顺序（每一步命中即返回）：
        1. 一次性票据（allowed-once）：绕过模式门与审批，用后即焚；
        2. 模式门：read-only 白名单命令直接放行（无风险，不惊动审批）；
           read-only 其余命令直接拒绝（写类命令在只读模式下没有商量）；
        3. 审批：never 直接拒绝；ask 调用审批回调（fail closed）。
        """
        # 1) 一次性票据：绕过一切，用后即焚
        if self._granted_once == command:
            self._granted_once = None
            return True, "allowed-once（一次性授权）"

        try:
            words = shlex.split(command)
        except ValueError as error:
            return False, f"[sandbox] 无法解析命令: {error}"
        first_word = words[0] if words else ""

        # 2) 模式门
        if self.mode == "read-only":
            if first_word in READ_ONLY_COMMANDS:
                return True, "read-only 白名单放行"
            return False, f"[sandbox] read-only 模式拒绝写类/未知命令: {command}"

        # 3) 审批
        if self.approval_policy == POLICY_NEVER:
            return False, "[approval] 审批策略为 never，直接拒绝"
        outcome = self.approver(command)
        if outcome == APPROVAL_ALLOWED_ONCE:
            return True, "approved（本轮放行）"
        if outcome == APPROVAL_CANCELLED:
            return False, "[approval] 审批被取消"
        if outcome == APPROVAL_UNAVAILABLE:
            return False, "[approval] 无可用审批通道（fail closed）"
        return False, "[approval] 审批被拒绝"

    def execute(self, command: str, cwd: str, timeout_seconds: float = 30.0) -> CommandResult:
        """决策 + 执行：先过门，再跑命令。"""
        allowed, reason = self.decide(command)
        if not allowed:
            return CommandResult(
                exit_code=1,
                stdout="",
                stderr=f"{reason}\n（命令未执行）",
            )
        # 教学版没有内核沙箱：只读白名单必须绕过 shell 解析，防止
        # `ls; rm file` 这类“首命令看似只读、后续命令产生写效应”的绕过。
        direct_read_only = reason == "read-only 白名单放行"
        return run_command(
            command,
            cwd,
            timeout_seconds,
            use_shell=not direct_read_only,
        )
