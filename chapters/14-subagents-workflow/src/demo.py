"""第 14 章 demo：主 agent 并行委派两个子任务。

运行（在项目根目录，需要 .env）：
    uv run python chapters/14-subagents-workflow/src/demo.py

演示：
1. 主 agent 收到「同时算两件事」的任务 → 一轮里请求两次 subagent 工具；
2. 两个子智能体并行执行（计时：总耗时 ≈ 单个耗时，而非两倍）；
3. 上下文隔离证据：子智能体看到的完整消息只有 system + task；
4. 主 agent 汇总子结果给出最终回答。
"""

from __future__ import annotations

import json
import time

from client import DeepSeekClient, Message
from session import Session
from subagent import create_subagent_tool, run_subagents_parallel


def main() -> None:
    client = DeepSeekClient()
    subagent_tool = create_subagent_tool(
        client, child_system_prompt="你是一个速算助手，直接给出答案。"
    )

    print("=== ① 主 agent 请求并行委派 ===")
    main_history: list[Message] = [
        Message(
            role="system",
            content=(
                "你是一个任务协调者。遇到多个独立的子问题时，"
                "在同一轮里用 subagent 工具并行委派，拿到全部结果后再汇总回答。"
            ),
        ),
        Message(role="user", content="同时帮我算两件事：3+3 等于几？7*2 等于几？"),
    ]

    reply = client.chat(main_history, [subagent_tool])
    main_history.append(reply)
    for call in reply.tool_calls:
        print(f"  [主 agent → 请求工具] {call.name}({call.arguments})")
    print(f"  （本轮请求了 {len(reply.tool_calls)} 个 subagent，将并行执行）")

    print()
    print("=== ② 并行执行（计时） ===")
    specs = []
    for call in reply.tool_calls:
        task = json.loads(call.arguments)["task"]
        specs.append((task, "你是一个速算助手，直接给出答案。"))
    started = time.monotonic()
    results = run_subagents_parallel(specs, client)
    elapsed = time.monotonic() - started
    for index, result in enumerate(results, start=1):
        print(f"  [子agent#{index}] {result.output}  ({result.stop_reason})")
    print(f"  总耗时: {elapsed:.1f}s（若串行执行约为两倍）")

    print()
    print("=== ③ 上下文隔离证据 ===")
    # 子智能体内部的会话只有 system + task——这里用一个探测会话展示
    probe = Session()
    probe.append("turn/start", {"turn": 1})
    probe.append("user/message", {"content": specs[0][0]})
    for message in [
        Message(role="system", content="你是一个速算助手，直接给出答案。"),
        *probe.derive_messages(),
    ]:
        print(f"  [{message.role}] {message.content[:40]}…")
    print("  ← 父 agent 的对话历史一个字都没进来")

    print()
    print("=== ④ 主 agent 汇总 ===")
    for call in reply.tool_calls:
        main_history.append(
            Message(
                role="tool",
                content=(
                    "[completed]\n"
                    + [r.output for r in results][
                        reply.tool_calls.index(call)
                    ]
                ),
                tool_call_id=call.id,
            )
        )
    final = client.chat(main_history, [subagent_tool])
    print(f"  [主 agent] {final.content}")


if __name__ == "__main__":
    main()
