"""第 05 章：日志化的 Agent 循环。

与第 02 章的唯一区别：对话历史从「普通 list」升级为「事件日志」。
循环不再直接改 messages 列表，而是往 Session 追加事件；
每次请求前用 derive_messages() 投影出模型看到的历史。
"""

from __future__ import annotations

import json

from client import DeepSeekClient, Message, Tool
from session import Session


def run_agent(
    client: DeepSeekClient,
    tools: list[Tool],
    system_prompt: str,
    user_prompt: str,
    max_turns: int = 10,
) -> Session:
    """跑一轮带工具调用的对话，全部过程记录进事件日志。"""
    tools_by_name = {tool.name: tool for tool in tools}
    session = Session()

    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": user_prompt})

    for turn in range(max_turns):
        # 关键：模型看到的历史永远是日志的「投影」，不是日志本身
        messages = [
            Message(role="system", content=system_prompt),
            *session.derive_messages(),
        ]
        reply = client.chat(messages, tools)
        session.append(
            "assistant/message",
            {
                "content": reply.content,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in reply.tool_calls
                ],
            },
        )

        if not reply.tool_calls:
            session.append("turn/end", {"turn": 1, "reason": "completed"})
            return session

        for call in reply.tool_calls:
            session.append(
                "tool/call",
                {"call_id": call.id, "name": call.name, "arguments": call.arguments},
            )
            tool = tools_by_name.get(call.name)
            if tool is None:
                result = f"Error: 模型请求了未注册的工具 {call.name!r}"
            else:
                try:
                    args = json.loads(call.arguments)
                    result = tool.execute(args)
                except Exception as error:
                    result = f"工具执行出错: {error}"
            session.append(
                "tool/result",
                {"call_id": call.id, "content": result, "is_error": "出错" in result},
            )

    session.append("turn/end", {"turn": 1, "reason": "max_turns"})
    raise RuntimeError(f"Agent 在 {max_turns} 轮内没有结束")
