"""第 02 章：工具调用循环 —— Agent 的核心骨架。

流程（这也是官方 Harness Agent Loop 的最简形态）：
    请求模型 → 模型要调用工具？→ 执行工具 → 结果回灌 → 再请求 → 模型作答 → 结束
"""

from __future__ import annotations

import json

from client import DeepSeekClient, Message, Tool


def run_agent(
    client: DeepSeekClient,
    tools: list[Tool],
    system_prompt: str,
    user_prompt: str,
    max_turns: int = 10,
) -> list[Message]:
    """跑一轮带工具调用的对话，返回完整历史（最后一条是模型的最终回答）。

    终止条件（教学版只保留最核心的两条）：
    1. 模型不再请求工具 —— 任务完成，返回历史；
    2. 达到 max_turns —— 安全阀，防止模型陷入「永远要调工具」的死循环。
    """
    tools_by_name = {tool.name: tool for tool in tools}
    history: list[Message] = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_prompt),
    ]

    for turn in range(max_turns):
        reply = client.chat(history, tools)
        history.append(reply)

        if not reply.tool_calls:
            return history  # 模型直接作答，任务完成

        # 模型请求了一组工具调用：逐个执行，结果以 role="tool" 回灌。
        # 关键细节：工具执行出错不中断 Agent，而是把错误文本回灌——
        # 模型看到错误后会自己修正（下一轮可能换个参数重试）。
        for call in reply.tool_calls:
            tool = tools_by_name.get(call.name)
            if tool is None:
                result = f"Error: 模型请求了未注册的工具 {call.name!r}"
            else:
                try:
                    args = json.loads(call.arguments)
                    result = tool.execute(args)
                except Exception as error:  # 参数解析或执行失败都回灌错误文本
                    result = f"工具执行出错: {error}"
            history.append(
                Message(role="tool", content=result, tool_call_id=call.id)
            )

    raise RuntimeError(f"Agent 在 {max_turns} 轮内没有结束")
