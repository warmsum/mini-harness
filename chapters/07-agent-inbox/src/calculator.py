"""安全 calculator 工具（第 02 章首次实现）。

教学要点：工具是「真实代码的入口」。模型只负责描述意图（"1+2*3"），
真正的计算在 Agent 进程里完成。安全边界划在这里：我们绝不用 eval，
而是手写一个递归下降解析器——模型传来的字符串是不可信输入。

语法（标准优先级，支持括号与一元负号）：
    expression := term (("+" | "-") term)*
    term       := factor (("*" | "/") factor)*
    factor     := number | "(" expression ")" | "-" factor
"""

from __future__ import annotations

from typing import Any

from client import Tool


def _evaluate(source: str) -> float:
    """把算术表达式求值为数字。非法输入抛错，错误信息会作为工具结果回灌给模型。"""
    tokens = _tokenize(source)
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        token = tokens[position]
        position += 1
        return token

    def parse_expression() -> float:
        value = parse_term()
        while peek() in ("+", "-"):
            operator = take()
            right = parse_term()
            value = value + right if operator == "+" else value - right
        return value

    def parse_term() -> float:
        value = parse_factor()
        while peek() in ("*", "/"):
            operator = take()
            right = parse_factor()
            if operator == "*":
                value *= right
            else:
                if right == 0:
                    raise ValueError("除数为零")
                value /= right
        return value

    def parse_factor() -> float:
        token = take()  # 末尾会抛 IndexError，由调用方转成错误信息
        if token == "(":
            value = parse_expression()
            if take() != ")":
                raise ValueError("缺少右括号")
            return value
        if token == "-":
            return -parse_factor()  # 一元负号：-x 等价于 0 - x
        return float(token)

    result = parse_expression()
    if position != len(tokens):
        raise ValueError(f"表达式在 {tokens[position]!r} 处意外结束")
    return result


def _tokenize(source: str) -> list[str]:
    """词法分析：把 "1+2*(3-1)" 拆成 ["1","+","2","*","(","3","-","1",")"]"""
    tokens: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
        elif char in "+-*/()":
            tokens.append(char)
            index += 1
        elif char.isdigit() or char == ".":
            number = ""
            while index < len(source) and (source[index].isdigit() or source[index] == "."):
                number += source[index]
                index += 1
            tokens.append(number)
        else:
            raise ValueError(f"非法字符: {char!r}")
    return tokens


def _run_calculator(args: dict[str, Any]) -> str:
    expression = args.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("参数 expression 必须是非空字符串")
    return str(_evaluate(expression))


calculator = Tool(
    name="calculator",
    description="计算一个四则运算表达式，支持 + - * / 与括号，例如 '1+2*3'",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式，例如 '1+2*(3-1)'",
            }
        },
        "required": ["expression"],
    },
    execute=_run_calculator,
)
