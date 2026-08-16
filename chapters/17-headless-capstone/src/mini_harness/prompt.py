"""第 06 章：系统提示词组装器。

对应官方 packages/core/system-prompt：插件可以贡献「有序段」、
工具 schema 和具名变量，循环在每个步骤组装一次。
教学版实现其中的段贡献与变量替换两个核心机制。
"""

from __future__ import annotations

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

    def section(self, name: str, text: str, order: int = 0) -> None:
        """贡献一段提示词。同名段重复贡献时后到者覆盖先到者
        （对应官方「同层重复名称抛出」的简化：教学版后者胜）。"""
        self._sections = [s for s in self._sections if s.name != name]
        self._sections.append(PromptSection(order=order, name=name, text=text))

    def render(self, variables: dict[str, str] | None = None) -> str:
        """按 order 排序拼接全部段，并替换 {{变量}} 占位符。

        变量机制（官方 :24 的 variable API）的用途：提示词里需要
        运行时才知道的值——模型名、当前目录、日期。段文本写
        {{model}}，组装时用真实值替换。
        """
        ordered = sorted(self._sections, key=lambda s: (s.order, s.name))
        text = "\n\n".join(section.text for section in ordered)
        for name, value in (variables or {}).items():
            text = text.replace("{{" + name + "}}", value)
        return text

    @property
    def sections(self) -> list[PromptSection]:
        return sorted(self._sections, key=lambda s: (s.order, s.name))
