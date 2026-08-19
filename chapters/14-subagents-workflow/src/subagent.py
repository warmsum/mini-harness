"""第 14 章：Subagent —— 把工作委派给子 agent。

对应官方 packages/subagent/subagent + tool-subagent。
教学版的核心决策（第 04 章 4.6 节约定的兑现）：
子 agent = 一个独立的运行环境：自己的会话、自己的工具子集，
只看到父 agent 交给它的 task 描述——父对话历史一个字都不带。

两个必须教的点：
1. 上下文隔离：子 agent 看不到父历史，这正是 subagent 省 token 的原因；
2. 并行：一轮里的多个子任务同时跑（官方按 isConcurrencySafe 并行调度）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from client import DeepSeekClient, Message, Tool
from session import Session


@dataclass(frozen=True)
class SubagentResult:
    """子 agent 的一次运行结果。

    对应官方 SubagentRun.result → { output, stopReason }：
    只有正常完成才返回 output；失败路径保留已生成的部分文本。"""

    output: str
    stop_reason: str  # "completed" / "max-steps" / "error"


def run_subagent(
    client: DeepSeekClient,
    task: str,
    system_prompt: str,
    max_steps: int = 3,
) -> SubagentResult:
    """运行一个子 agent：独立的 Session，只见 task，不见父历史。

    上下文隔离是这里的关键——父 agent 的对话历史可能有几万 token，
    而一个子任务往往只需要一句 task 描述。把历史挡在门外，
    每个子 agent 的输入都从零开始（官方 fork 是例外，见本章对照表）。"""
    session = Session()
    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": task})

    partial: str = ""
    try:
        for _step in range(max_steps):
            reply = client.chat(
                [
                    Message(role="system", content=system_prompt),
                    *session.derive_messages(),
                ]
            )
            session.append(
                "assistant/message",
                {"content": reply.content, "tool_calls": []},
            )
            partial = reply.content or ""
            if reply.content:
                session.append("turn/end", {"turn": 1, "reason": "completed"})
                return SubagentResult(output=reply.content, stop_reason="completed")
        return SubagentResult(
            output=partial,
            stop_reason="max-steps",
        )
    except Exception as error:
        # 失败保留部分文本：被截断的回答不会被报告为成功，
        # 也不会被悄悄丢弃。
        return SubagentResult(output=partial, stop_reason=f"error: {error}")


def run_subagents_parallel(
    specs: list[tuple[str, str]],  # (task, system_prompt)
    client: DeepSeekClient,
) -> list[SubagentResult]:
    """并行运行多个子 agent。

    DeepSeekClient.chat 是同步阻塞调用（等待网络），用线程池并行——
    多个子任务同时跑，总耗时 ≈ 最慢的那个，而不是逐个相加。
    官方在同一轮里对 isConcurrencySafe 的工具调用做并行调度，
    这里是对「多个 subagent 调用」的教学版并行。"""
    if not specs:
        return []
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        futures = [
            pool.submit(run_subagent, client, task, system_prompt)
            for task, system_prompt in specs
        ]
        return [future.result() for future in futures]


def create_subagent_tool(
    client: DeepSeekClient, child_system_prompt: str
) -> Tool:
    """把 run_subagent 包装成第 02 章风格的 Tool。

    官方把「委派」做成一个基于已配置 provider 的模型工具——模型在需要
    拆分任务时主动调用它，参数就是子任务描述。"""
    def execute(args: dict[str, Any]) -> str:
        task = args.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("参数 task 必须是非空字符串")
        result = run_subagent(client, task, child_system_prompt)
        return f"[{result.stop_reason}]\n{result.output}"

    return Tool(
        name="subagent",
        description=(
            "把一个独立的子任务委派给子 agent 执行并等待其完成。"
            "子 agent 看不到当前对话历史，只看到 task 描述。"
            "适合：独立的小任务、并行探索多个方向。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要委派的子任务描述，要自包含（子 agent 没有上下文）",
                }
            },
            "required": ["task"],
        },
        execute=execute,
    )
