"""第 10 章：沙箱围栏 —— 文件写入的边界。

对应官方 packages/fs/fs-sandbox。
核心概念：三模式沙箱只约束「文件写效应」——读永远放行，
写必须落在可写根目录（工作区根 + 临时目录）内。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

# 三模式词汇（对应官方 sandbox 的模式表）
SandboxMode = str
READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"
DANGER_FULL_ACCESS = "danger-full-access"

# 严格更宽升级表：升级只能「向更宽」，read-only 是地板。
WIDER_MODES: dict[str, tuple[str, ...]] = {
    READ_ONLY: (WORKSPACE_WRITE, DANGER_FULL_ACCESS),
    WORKSPACE_WRITE: (DANGER_FULL_ACCESS,),
    DANGER_FULL_ACCESS: (),
}


class SandboxDeniedError(PermissionError):
    """结构化拒绝：携带有效模式（对应官方 FsError FS_SANDBOX_DENIED）。"""

    def __init__(self, path: str, mode: str) -> None:
        self.mode = mode
        super().__init__(f"[sandbox: file access denied under {mode} mode]: {path}")


@dataclass(frozen=True)
class SandboxPolicy:
    """沙箱策略：一个模式 + 一个工作区根。"""

    mode: str = READ_ONLY
    workspace_root: Path = Path.cwd()

    def writable_roots(self) -> list[Path]:
        """可写根目录集合：工作区根 + 平台临时目录。
        （对应官方 writableRoots 派生集合。）"""
        return [
            self.workspace_root,
            Path(tempfile.gettempdir()),
            Path("/tmp"),
        ]

    def fence_write(self, target: Path) -> Path:
        """写前围栏：目标规范化后必须位于某个可写根之下，否则拒绝。

        规范化（resolve）是核心——攻击路径 `workspace/../etc/passwd`
        在词法上逃出工作区，resolve 后无处遁形。
        诚实边界：这是「约束」而非「安全边界」，
        真正的内核级隔离属于第 11 章的 shell 沙箱。"""
        if self.mode == DANGER_FULL_ACCESS:
            return target
        if self.mode == READ_ONLY:
            raise SandboxDeniedError(str(target), self.mode)
        if self.mode != WORKSPACE_WRITE:
            raise ValueError(f"未知 sandbox mode: {self.mode}")
        resolved = target.resolve()
        for root in self.writable_roots():
            root_resolved = root.resolve()
            try:
                resolved.relative_to(root_resolved)
                return resolved
            except ValueError:
                continue
        raise SandboxDeniedError(str(target), self.mode)


def approve_escalation(policy: SandboxPolicy, requested: str) -> SandboxPolicy:
    """审批升级：请求更宽模式，只有「严格更宽」才可能获批。
    教学版没有人工审批通道，按表直接放行；第 11 章接上真实审批询问。"""
    if requested in WIDER_MODES[policy.mode]:
        return SandboxPolicy(mode=requested, workspace_root=policy.workspace_root)
    raise SandboxDeniedError(
        f"cannot escalate from {policy.mode} to {requested}", policy.mode
    )
