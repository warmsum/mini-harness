"""第 07 章：Agent —— 持续对话的完整循环。

第 06 章的 run_agent 是「一次性」的：一个问题进，一个结果出。
本章的 Agent 是「常驻」的：随时接收 followup/steer，逐轮处理，
轮次之间保持同一份会话日志——这正是官方 AgentLoop 的形态。

层级术语（与官方对齐）：
- turn（轮次）：一次「唤醒到完成」的边界，由 turn/start 与 turn/end 夹住；
- step（步骤）：一轮内部的一次「模型调用 + 工具执行」。
"""

from __future__ import annotations

import json

from .client import DeepSeekClient, Message, Tool
from .inbox import Inbox
from .prompt import PromptAssembler
from .registry import ToolRegistry
from .session import Session


class Agent:
    def __init__(
        self,
        client: DeepSeekClient,
        registry: ToolRegistry,
        assembler: PromptAssembler,
        variables: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._assembler = assembler
        self._variables = variables
        self._inbox = Inbox()
        self._session = Session()
        self._turn_no = 0
        self._request_header: str | None = None

    # ------------------------------------------------------------------
    # 外部入口：投递消息
    # ------------------------------------------------------------------

    def followup(self, content: str) -> None:
        """用户的常规提问：进入下一轮队列。"""
        self._inbox.followup(Message(role="user", content=content))

    def steer(self, content: str) -> None:
        """中途引导：进入下一步队列，当前轮次内立刻生效。"""
        self._inbox.steer(Message(role="user", content=content))

    @property
    def session(self) -> Session:
        return self._session

    # ------------------------------------------------------------------
    # 主循环：领取 → 轮次 → 步骤
    # ------------------------------------------------------------------

    def run(self, max_turns: int = 5) -> Session:
        """处理 inbox 直到没有待处理消息（教学版同步实现；
        官方在这里是常驻驱动器，空闲时挂起等待唤醒）。"""
        tools = self._registry.all()
        tools_by_name = {tool.name: tool for tool in tools}

        while self._inbox.pending > 0 and self._turn_no < max_turns:
            claimed = self._inbox.claim_turn()
            if not claimed:
                break
            self._turn_no += 1
            self._session.append("turn/start", {"turn": self._turn_no})
            try:
                self._run_turn(tools, tools_by_name, claimed)
            except Exception as error:
                self._session.append(
                    "turn/end",
                    {"turn": self._turn_no, "reason": "error", "message": str(error)},
                )
                raise
            else:
                self._session.append(
                    "turn/end", {"turn": self._turn_no, "reason": "completed"}
                )
        return self._session

    def _run_turn(
        self,
        tools: list[Tool],
        tools_by_name: dict[str, Tool],
        claimed: list[Message],
    ) -> None:
        """一轮内部：反复「领 steer → 请求模型 → 执行工具」直到模型作答。"""
        for step in range(1, 11):  # 安全阀：单轮最多 10 个 step
            if step > 1:
                claimed = self._inbox.claim_step()
            self._session.append("step/start", {"turn": self._turn_no, "step": step})
            completed = False
            try:
                for message in claimed:
                    self._session.append("user/message", {"content": message.content})

                system_prompt = self._assembler.render(self._variables)
                header = {
                    "config": {"provider": "deepseek", "model": self._client.MODEL},
                    "system": system_prompt,
                    "tools": self._registry.schemas(),
                }
                header_fingerprint = json.dumps(
                    header, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                if header_fingerprint != self._request_header:
                    self._session.append(
                        "request/header",
                        {
                            "header": header,
                            "reason": "initial" if self._request_header is None else "change",
                        },
                    )
                    self._request_header = header_fingerprint

                messages = [
                    Message(role="system", content=system_prompt),
                    *self._session.derive_messages(),
                ]
                reply = self._client.chat(messages, tools)
                self._session.append(
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
                        self._session.append(
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
                        self._session.append(
                            "tool/result",
                            {"call_id": call.id, "content": result, "is_error": is_error},
                        )
            finally:
                self._session.append(
                    "step/end", {"turn": self._turn_no, "step": step}
                )

            if completed and not self._inbox.has_next_step:
                return
        raise RuntimeError(f"第 {self._turn_no} 轮超过 10 个 step 仍未结束")
