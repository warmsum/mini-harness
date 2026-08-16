"""mini_harness 入口：一次性任务运行器。

用法（在 src 目录下运行）：
    python -m mini_harness "你的任务"

对应官方 bundle/headless 的 runner 语义（headless 文档第 7 行）：
创建 Agent、把任务作为普通用户消息提交、等待完全停稳、
把最后一条 assistant 文本写入 stdout；最终 turn/end 完成 → 退出码 0，
否则 1。进程不打开任何监听端口。
"""

from __future__ import annotations

import sys

from .agent import Agent
from .calculator import calculator
from .client import DeepSeekClient
from .meter import TokenMeter
from .persistence import JsonlStore
from .prompt import PromptAssembler
from .registry import ToolRegistry

SESSION_FILE = "session.jsonl"


def build_agent() -> Agent:
    """组装 Agent：第 06 章的信封 + 第 07 章的循环 + 第 02 章的工具。"""
    assembler = PromptAssembler()
    assembler.section(
        "persona",
        "你是 {{name}}，一个编程助手。遇到算式时先调用 calculator 工具计算，"
        "再基于计算结果回答。",
        order=0,
    )
    assembler.section("rules", "回答要简洁：先给结论，再给过程。", order=100)

    registry = ToolRegistry()
    registry.register(calculator)

    return Agent(
        DeepSeekClient(),
        registry,
        assembler,
        variables={"name": "小算"},
    )


def run_task(task: str, session_file: str = SESSION_FILE) -> tuple[str, bool]:
    """跑一个一次性任务，返回 (最后一条 assistant 文本, 是否正常完成)。"""
    agent = build_agent()
    meter = TokenMeter(context_window=100_000)

    agent.followup(task)
    session = agent.run()

    # 第 09 章的计量：任务结束后汇报上下文压力
    pressure = meter.pressure(meter.measure(_messages_of(session)))
    print(f"[meter] 上下文占用 {pressure.ratio:.1%}", file=sys.stderr)

    # 第 08 章的持久化：会话落盘
    store = JsonlStore(session_file)
    store.save(session)
    print(f"[persist] 会话已保存到 {store.path}", file=sys.stderr)

    # 官方语义：最后一条 assistant 文本写 stdout；完成与否决定退出码
    final_text = ""
    completed = False
    for message in session.derive_messages():
        if message.role == "assistant" and message.content:
            final_text = message.content
    for event in session.events:
        if event.type == "turn/end" and event.data.get("reason") == "completed":
            completed = True
    return final_text, completed


def _messages_of(session) -> list:
    from .client import Message

    system = Message(role="system", content="(组装提示词)")
    return [system, *session.derive_messages()]


def main() -> None:
    task = " ".join(sys.argv[1:]).strip()
    if not task:
        print("用法: python -m mini_harness \"你的任务\"", file=sys.stderr)
        sys.exit(2)
    final_text, completed = run_task(task)
    if final_text:
        print(final_text)
    sys.exit(0 if completed else 1)


if __name__ == "__main__":
    main()
