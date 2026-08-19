# 07｜常驻 Agent 与 inbox

> 预计时间：65 分钟 ｜ 前置：完成第 06 章 ｜ 本章调用真实 DeepSeek 模型

第 06 章的 `run_agent` 只处理一个问题，返回结果后便结束。真实会话通常包含多次连续提问，Agent 需要保留已有上下文；如果用户在 Agent 执行工具期间补充要求，新消息还需要在明确的时机生效。本章把一次性函数改造成可处理多轮消息的常驻 Agent，并回答两个问题：

1. 新消息在 Agent 忙碌时到达，该怎么排队、何时生效？
2. 一次 Agent 运行里，哪些算一轮，哪些算一步？

官方把答案放在 core/agent-loop：Agent 的 inbox 按下一轮和下一步两条队列分流消息，循环本身只做调用模型、运行工具、重复这一件事，其余全部由边界事件组织。本章复刻这套结构的教学版。

## 学习目标

完成本章后，你将能够：

- 准确区分 turn（轮次）与 step（步骤）；
- 使用 inbox 的 next-turn 与 next-step 队列安排消息；
- 让同一个 Agent 连续处理多轮输入并保留会话历史；
- 说明 followup 与 steer 的生效时机为什么不同。

## 7.1 轮次与步骤

先明确本章会反复使用的两个术语。turn（轮次）表示 Agent 从接收一条输入到完成回答的一次完整运行，对应事件日志中 `turn/start` 与 `turn/end` 之间的区间。step（步骤）表示轮次内部的一次模型调用及其后续工具执行；如果模型需要连续调用工具，一个轮次就会包含多个 step。

新消息根据生效时机进入不同队列：

| 消息类型 | 进入的队列 | 领取时机 |
|----------|------------|----------|
| followup | next-turn | 当前轮次结束后，作为下一轮输入 |
| steer | next-step | 当前轮次的下一个 step 开始前 |

轮次为什么重要，两个实际用途：

1. 轮次边界适合执行维护操作。压缩（第 09 章）、持久化（第 08 章）可以安排在这里，因为此时没有进行到一半的模型调用或工具调用。
2. 账单与审计的粒度。一次任务包含几个轮次、每轮包含几个步骤，是分析成本的基本单位。官方事件日志会完整记录 turn/start 与 turn/end。

## 7.2 Inbox：两条队列，两个领取时机

```python
class Inbox:
    def __init__(self) -> None:
        self._next_turn: deque[Message] = deque()  # 下一轮
        self._next_step: deque[Message] = deque()  # 下一步

    def followup(self, message: Message) -> None:
        self._next_turn.append(message)

    def steer(self, message: Message) -> None:
        self._next_step.append(message)

    def claim_turn(self) -> list[Message]:
        claimed = list(self._next_step)
        self._next_step.clear()
        if self._next_turn:
            claimed.append(self._next_turn.popleft())
        return claimed

    def claim_step(self) -> list[Message]:
        claimed = list(self._next_step)
        self._next_step.clear()
        return claimed
```

三个细节：

- `deque` 是标准库的双端队列，`popleft` 从头取，FIFO 顺序保证先到先处理。
- `claim_turn` 先原子领取全部 `_next_step`，再领取一条 `_next_turn`；steer 因此排在排队 prompt 之前。`claim_step` 也一次领取整个 next-step 批次，不会人为拆成多次模型调用。
- 教学版的 `claim_step` 只领不检查唤醒。官方两条队列的投递都带唤醒语义，inject 是第三个别名，投递到 next-step 但不唤醒，留给练习 2。

## 7.3 Agent：常驻循环

Agent 持有 inbox、会话日志和请求 envelope，对外只暴露两个入口，`followup` 投递常规问题，`steer` 投递中途引导：

```python
class Agent:
    def __init__(self, client, registry, assembler, variables=None):
        self._client = client
        self._registry = registry
        self._assembler = assembler
        self._variables = variables
        self._inbox = Inbox()
        self._session = Session()
        self._turn_no = 0

    def followup(self, content: str) -> None:
        self._inbox.followup(Message(role="user", content=content))

    def steer(self, content: str) -> None:
        self._inbox.steer(Message(role="user", content=content))

    def run(self, max_turns: int = 5) -> Session:
        tools = self._registry.all()
        tools_by_name = {tool.name: tool for tool in tools}

        while self._inbox.pending > 0 and self._turn_no < max_turns:
            claimed = self._inbox.claim_turn()
            if not claimed:
                break
            self._turn_no += 1
            self._session.append("turn/start", {"turn": self._turn_no})
            self._run_turn(tools, tools_by_name, claimed)
            self._session.append(
                "turn/end", {"turn": self._turn_no, "reason": "completed"}
            )
        return self._session
```

主循环按固定顺序运行：领取一条消息、记录轮次开始、执行本轮、记录轮次结束，直到 inbox 清空。第 06 章的循环体移入 `_run_turn` 后，只增加了一个步骤：

```python
    def _run_turn(self, tools, tools_by_name, claimed) -> None:
        for step in range(1, 11):
            if step > 1:
                claimed = self._inbox.claim_step()
            self._session.append("step/start", {"turn": self._turn_no, "step": step})
            try:
                for message in claimed:
                    self._session.append("user/message", {"content": message.content})
                # 记录 request/header，请求模型并执行工具
                ...
            finally:
                self._session.append("step/end", {"turn": self._turn_no, "step": step})
            if completed and not self._inbox.has_next_step:
                return
```

每个 step 都有明确边界。即使模型已经给出最终文本，只要请求期间又到了一批 steer，当前 turn 就继续下一个 step，而不是先结束 turn 再另开一轮。这一点是 steer“当前轮立即生效”的关键。

教学版仍是同步驱动器：有消息就跑到 inbox 暂时清空；官方会在空闲时挂起并由投递唤醒。官方还持久记录 inbox splice、取消、blocked、max-tokens 等完整终止原因，本章只保留完成、错误与 turn 数安全阀。

## 7.4 运行完整示例

```bash
uv run python chapters/07-agent-inbox/src/demo.py
```

真实输出，模型回复内容每次不同，事件结构稳定：

```
=== 第 1 轮：问 1+2*3 ===
  [assistant] 1+2×3 = **7**

按运算顺序，先算乘法 2×3=6，再算加法 1+6=7。

=== 第 2 轮：followup 再问 8/4（同一会话，历史延续） ===
  [assistant] 1+2×3 = **7**

按运算顺序，先算乘法 2×3=6，再算加法 1+6=7。
  [assistant] 8÷4 = **2**

=== 事件日志：两个轮次边界 ===
  #0  turn/start            ← 轮次边界
  #1  user/message
  #2  assistant/message
  #3  tool/call
  #4  tool/result
  #5  assistant/message
  #6  turn/end              ← 轮次边界
  #7  turn/start            ← 轮次边界
  #8  user/message
  #9  assistant/message
  #10 tool/call
  #11 tool/result
  #12 assistant/message
  #13 turn/end              ← 轮次边界
```

三个观察点：

1. 历史延续。第 2 轮打印出的消息里有两条 assistant，第一条是第 1 轮的答案。派生视图自动包含全部历史，模型在第 2 轮确实记得上一轮发生了什么。
2. 边界清晰。14 条事件被分成两个由 turn/start 与 turn/end 夹住的完整块，这是轮次边界在日志里的样子，第 08 章与第 09 章会反复用到它。
3. 每轮两个 step。一次模型调用加工具执行就是一个 step，观察 assistant/message 与 tool/call 的交替，一轮里交替几次就是几个 step。

## 本章小结

- `Inbox`：next-turn 与 next-step 两条队列，两个领取时机
- `Agent`：常驻循环，领取、开轮、轮内 step 循环、关轮
- `steer` 机制：step 级插队，带标记记录进日志
- turn（轮次）与 step（步骤）术语体系，以及轮次边界在日志中的形态

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/core/agent-loop/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/README.zh.md) | 术语 | 官方同样用 turn 表示一次唤醒到结束，用 step 表示一次模型调用与工具执行 |
| 同上 | `Inbox` | 官方 `send()` 按 target × wakeup 路由；followup、steer、inject 分别表达下一轮、下一步唤醒和下一步静默注入 |
| 同上 | `Agent` | 官方把 ReactLoopAgent 与 inbox 保持为包内实现，对外暴露 send 原语；教学版直接暴露类便于学习 |
| 同上 | `_run_turn` | 核心循环只负责调用模型、运行工具和重复，其余行为由插件与事件组合 |
| 同上 | 会话日志 | 已接纳消息、请求边界与工具调用写入日志，并用于后续 step 的请求重建 |

## 练习

1. **steer 实战。** 先 followup 一个算式问题，在 run 之前再 steer 一条用中文回答、不要用 LaTeX 公式，对比有与没有 steer 时模型输出的风格差异。
2. **inject 补全。** 仿照官方第三种原语 inject，给 Inbox 加一个投递到 next-step 但不唤醒的入口，思考它适合什么场景。
3. **轮次推演。** 纸笔推演 demo 两个轮次的完整事件序列，标注每个 step 的边界，与真实输出对比。
4. **max_turns 行为。** 把 max_turns 设为 1，投递两个 followup，观察第二条消息去哪了，思考真实框架该如何处理没处理完的消息。
