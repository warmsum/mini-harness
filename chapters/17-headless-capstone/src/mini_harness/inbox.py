"""第 07 章：Inbox —— 两条待处理队列与领取原语。

对应官方 core/agent-loop 文档第 58 行的 inbox 设计：
- followup：追加到「下一轮」队列，并唤醒 Agent；
- steer：追加到「下一步」队列——当前轮次内、下一次模型请求前生效。

两者的区别是时机：followup 等当前轮次结束才处理；steer 中途插队，
下一个 step 就让模型看到。这正是人类协作里「发消息等回复」与
「站在同事旁边当场喊一句」的区别。
"""

from __future__ import annotations

from collections import deque

from .client import Message


class Inbox:
    """两条待处理队列 + 领取原语。"""

    def __init__(self) -> None:
        self._next_turn: deque[Message] = deque()  # 下一轮：轮次边界处领取
        self._next_step: deque[Message] = deque()  # 下一步：每个 step 开始前领取

    def followup(self, message: Message) -> None:
        """投递一条「下一轮」消息（用户的常规提问）。"""
        self._next_turn.append(message)

    def steer(self, message: Message) -> None:
        """投递一条「下一步」消息（中途引导/纠正）。"""
        self._next_step.append(message)

    def claim_turn(self) -> Message | None:
        """轮次边界处领取：优先 next-turn，其次 next-step。
        （步骤级输入也可能开新轮——比如上轮结束时恰好插队进来一条。）"""
        if self._next_turn:
            return self._next_turn.popleft()
        if self._next_step:
            return self._next_step.popleft()
        return None

    def claim_step(self) -> Message | None:
        """每个 step 开始前领取：只有 next-step 队列。"""
        if self._next_step:
            return self._next_step.popleft()
        return None

    @property
    def pending(self) -> int:
        return len(self._next_turn) + len(self._next_step)
