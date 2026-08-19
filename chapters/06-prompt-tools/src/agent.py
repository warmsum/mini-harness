"""第 06 章：使用组装提示词与注册表的 Agent 循环。

与第 05 章的两处差异：
1. 工具从 ToolRegistry 来（不再是一张裸 list）；
2. 系统提示词由 PromptAssembler 组装（不再是手写一整块）。
"""

from __future__ import annotations

import json

from client import DeepSeekClient, Message
from prompt import PromptAssembler
from registry import ToolRegistry
from session import Session


def run_agent(
    client: DeepSeekClient,
    registry: ToolRegistry,
    assembler: PromptAssembler,
    user_prompt: str,
    max_steps: int = 10,
    variables: dict[str, str] | None = None,
) -> Session:
    """跑一轮带工具调用的对话。请求 envelope = 组装出的 system + 注册表 schema。"""
    tools = registry.all()
    tools_by_name = {tool.name: tool for tool in tools}
    session = Session()

    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": user_prompt})
    system_prompt = assembler.render(variables)
    session.append(
        "request/header",
        {
            "header": {
                "config": {"provider": "deepseek", "model": client.MODEL},
                "system": system_prompt,
                "tools": registry.schemas(),
            },
            "reason": "initial",
        },
    )

    try:
        for step in range(1, max_steps + 1):
            session.append("step/start", {"turn": 1, "step": step})
            completed = False
            try:
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
                    completed = True
                else:
                    for call in reply.tool_calls:
                        session.append(
                            "tool/call",
                            {
                                "call_id": call.id,
                                "name": call.name,
                                "arguments": call.arguments,
                            },
                        )
                        tool = tools_by_name.get(call.name)
                        is_error = tool is None
                        if tool is None:
                            result = f"Error: 模型请求了未注册的工具 {call.name!r}"
                        else:
                            try:
                                args = json.loads(call.arguments)
                                result = tool.execute(args)
                            except Exception as error:
                                is_error = True
                                result = f"工具执行出错: {error}"
                        session.append(
                            "tool/result",
                            {"call_id": call.id, "content": result, "is_error": is_error},
                        )
            finally:
                session.append("step/end", {"turn": 1, "step": step})
            if completed:
                session.append("turn/end", {"turn": 1, "reason": "completed"})
                return session
    except Exception as error:
        session.append(
            "turn/end", {"turn": 1, "reason": "error", "message": str(error)}
        )
        raise

    session.append("turn/end", {"turn": 1, "reason": "max-steps"})
    raise RuntimeError(f"Agent 在 {max_steps} 个 step 内没有结束")
