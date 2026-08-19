"""第 06 章：工具注册表。

第 02 章的工具只是一张 list[Tool]。真实 Agent 里工具需要管理：
注册（查重）、注销、以及「给模型看的说明书清单」——模型只需要
name/description/parameters，execute 是给程序跑的，绝不能外泄。
"""

from __future__ import annotations

from typing import Any

from client import Tool


class ToolRegistry:
    """工具注册表：登记、查找、投影说明书。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。重名即抛错——两个同名工具会让模型传参时
        产生歧义，必须在入口处挡掉。"""
        if tool.name in self._tools:
            raise ValueError(f'工具 "{tool.name}" 已被注册')
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        del self._tools[name]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return [self._tools[name] for name in sorted(self._tools)]

    def schemas(self) -> list[dict[str, Any]]:
        """投影出「给模型看的说明书清单」：只有 name/description/
        parameters 三个字段，不含 execute。这是注册表与普通列表的关键
        区别——模型侧接口与程序侧接口被明确分开。"""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self.all()
        ]
