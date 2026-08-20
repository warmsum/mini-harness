"""系统提示词组装器（第 06 章首次实现）。

对应官方 packages/core/system-prompt：插件可以贡献「有序段」、
工具 schema 和具名变量，循环在每个步骤组装一次。
教学版实现其中的段贡献与变量替换两个核心机制。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSection:
    """系统提示词里的一段。order 决定排序（数字小的在前）。"""

    order: int
    name: str
    text: str


class PromptAssembler:
    """把多个贡献者提供的提示词段组装成完整系统提示词。

    为什么系统提示词要「组装」而不是一块写死？因为真实 Agent 的
    提示词来自多个插件：人设插件贡献一段人设、工具插件贡献工具目录、
    沙箱插件贡献安全规则……每个插件只管自己那一段，组装器负责排序
    拼接。官方把这种贡献叫 section（段），顺序由 order 字段决定。
    """

    def __init__(self) -> None:
        self._sections: list[PromptSection] = []
        self._variables: dict[str, Callable[[], str]] = {}

    def section(self, name: str, text: str, order: int = 0) -> Callable[[], None]:
        """贡献一段提示词。同层同名段会立即报错。"""
        if any(section.name == name for section in self._sections):
            raise ValueError(f'提示词段 "{name}" 已被注册')
        contribution = PromptSection(order=order, name=name, text=text)
        self._sections.append(contribution)

        def remove() -> None:
            if contribution in self._sections:
                self._sections.remove(contribution)

        return remove

    def variable(self, name: str, provider: Callable[[], str]) -> Callable[[], None]:
        if name in self._variables:
            raise ValueError(f'提示词变量 "{name}" 已被注册')
        self._variables[name] = provider

        def remove() -> None:
            if self._variables.get(name) is provider:
                del self._variables[name]

        return remove

    def render(self, variables: dict[str, str] | None = None) -> str:
        """按 order 排序拼接全部段，并替换 {{变量}} 占位符。

        变量 provider 的用途：提示词里需要
        运行时才知道的值——模型名、当前目录、日期。段文本写
        {{model}}，组装时用真实值替换。
        """
        ordered = sorted(self._sections, key=lambda s: s.order)
        text = "\n\n".join(section.text for section in ordered)
        resolved = {name: provider() for name, provider in self._variables.items()}
        resolved.update(variables or {})
        for name, value in resolved.items():
            text = text.replace("{{" + name + "}}", value)
        unresolved = sorted(set(re.findall(r"{{([a-zA-Z_][a-zA-Z0-9_]*)}}", text)))
        if unresolved:
            raise KeyError(f"未注册的提示词变量: {', '.join(unresolved)}")
        return text

    @property
    def sections(self) -> tuple[PromptSection, ...]:
        return tuple(sorted(self._sections, key=lambda s: s.order))
