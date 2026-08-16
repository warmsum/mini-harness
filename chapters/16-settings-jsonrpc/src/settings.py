"""第 16 章：Settings —— 分层配置。

对应官方 packages/settings/settings。
教学版实现配置的三个基本要求：
1. 默认值兜底：缺失的配置项有 sane default；
2. 文件覆盖默认：.env 提供本地值（不提交进 git）；
3. 显式覆盖文件：程序调用方传入的值最高优先。
"""

from __future__ import annotations

from pathlib import Path


class Settings:
    """分层配置：显式覆盖 > .env > 默认值。"""

    def __init__(
        self,
        env_path: Path | None = None,
        defaults: dict[str, str] | None = None,
        overrides: dict[str, str] | None = None,
    ) -> None:
        self._defaults = dict(defaults or {})
        self._env: dict[str, str] = {}
        self._overrides = dict(overrides or {})
        if env_path is not None and env_path.exists():
            self._load_env(env_path)

    # ------------------------------------------------------------------
    # 读取：三层逐级回落
    # ------------------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        """按优先级取：显式覆盖 > .env > 构造默认 > 参数默认。"""
        if key in self._overrides:
            return self._overrides[key]
        if key in self._env:
            return self._env[key]
        if key in self._defaults:
            return self._defaults[key]
        return default

    def set(self, key: str, value: str) -> None:
        """显式覆盖（程序调用方传入，最高优先）。"""
        self._overrides[key] = value

    def as_dict(self) -> dict[str, str]:
        """合并视图：三层压平成一张表（供展示与 RPC 返回）。"""
        merged = dict(self._defaults)
        merged.update(self._env)
        merged.update(self._overrides)
        return merged

    # ------------------------------------------------------------------
    # 内部：.env 解析
    # ------------------------------------------------------------------

    def _load_env(self, path: Path) -> None:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            self._env[key.strip()] = value.strip().strip('"').strip("'")
