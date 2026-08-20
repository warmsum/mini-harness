"""mini_harness 入口：一次性任务运行器。

用法（在项目根目录运行已声明的命令行入口）：
    uv run mini-harness "你的任务"

也可以在本章 src 目录运行：
    uv run python -m mini_harness "你的任务"

对应官方 bundle/headless 的 runner 语义：
创建 Agent、把任务作为普通用户消息提交、等待完全停稳、
把最后一条 assistant 文本写入 stdout；最终 turn/end 完成 → 退出码 0，
否则 1。进程不打开任何监听端口。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import uuid4

from .agent import Agent
from .bundle import BundleConfig, headless_bundle
from .capabilities import load_settings_document
from .client import Message
from .cordis import Context
from .meter import TokenMeter
from .persistence import JsonlStore
from .session import Session

SESSION_DIR = Path(".mini-harness") / "sessions"


def build_agent(
    *,
    enable_console_questions: bool = True,
    checkpoint_flush: Callable[[Session], None] | None = None,
) -> Agent:
    """创建 Context 并挂载 headless Bundle；能力由插件依赖自动接线。"""
    ctx = Context()
    ctx.plugin(
        headless_bundle,
        BundleConfig(
            settings_document=load_settings_document(),
            enable_console_questions=enable_console_questions,
            checkpoint_flush=checkpoint_flush,
        ),
    )
    return cast(Agent, ctx.require("agent"))


def run_task(task: str, session_file: str | Path | None = None) -> tuple[str, bool]:
    """跑一个一次性任务，返回 (最后一条 assistant 文本, 是否正常完成)。"""
    path = Path(session_file) if session_file is not None else _new_session_file()
    store = JsonlStore(path)
    agent = build_agent(checkpoint_flush=store.save)
    meter = TokenMeter(context_window=100_000)

    try:
        agent.followup(task)
        session = agent.run()
        # 第 09 章的计量：任务结束后汇报上下文压力
        pressure = meter.pressure(meter.measure(_messages_of(session)))
        print(f"[meter] 上下文占用 {pressure.ratio:.1%}", file=sys.stderr)

        # 官方语义：最后一条 assistant 文本写 stdout；完成与否决定退出码
        final_text = ""
        for message in session.derive_messages():
            if message.role == "assistant" and message.content:
                final_text = message.content
        turn_ends = [event for event in session.events if event.type == "turn/end"]
        completed = bool(turn_ends and turn_ends[-1].data.get("reason") == "completed")
        return final_text, completed
    finally:
        try:
            store.save(agent.session)
            print(f"[persist] 会话已保存到 {store.path}", file=sys.stderr)
        finally:
            agent.close()


def _new_session_file() -> Path:
    """为一次性任务分配新日志，避免下一次运行覆盖上一份会话。"""
    return SESSION_DIR / f"{uuid4().hex}.jsonl"


def _messages_of(session: Session) -> list[Message]:
    system = Message(role="system", content="(组装提示词)")
    return [system, *session.derive_messages()]


def main() -> None:
    arguments = sys.argv[1:]
    if arguments == ["--rpc"]:
        _run_rpc()
        return
    task = " ".join(arguments).strip()
    if not task:
        print('用法: mini-harness "你的任务"', file=sys.stderr)
        sys.exit(2)
    final_text, completed = run_task(task)
    if final_text:
        print(final_text)
    sys.exit(0 if completed else 1)


def _run_rpc() -> None:
    """stdio 上的 JSON-RPC line transport；不打开监听端口。"""
    store = JsonlStore(_new_session_file())
    agent = build_agent(
        enable_console_questions=False,
        checkpoint_flush=store.save,
    )
    dispatcher = agent.rpc_dispatcher
    if dispatcher is None:
        raise RuntimeError("JSON-RPC dispatcher 未组装")
    try:
        for line in sys.stdin:
            response = dispatcher.dispatch(line)
            print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        try:
            store.save(agent.session)
        finally:
            agent.close()


if __name__ == "__main__":
    main()
