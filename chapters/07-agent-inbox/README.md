# 07｜常驻 Agent 与 inbox

> 预计时间：65 分钟 ｜ 前置：完成第 06 章 ｜ 本章调用真实 DeepSeek 模型

第 06 章的 `run_agent` 是一次性的：一个问题进去，一个结果出来，函数返回后
Agent 就结束了。真实的使用方式不是这样。一个会话里，用户会连续问好几个
问题，每问一次，Agent 都要接着之前的上下文继续工作；更微妙的是，Agent
正在干活时，用户可能突然插一句停，换个思路。本章把一次性函数升级成常驻
的 Agent，并回答两个问题：

1. 新消息在 Agent 忙碌时到达，该怎么排队、何时生效？
2. 一次 Agent 运行里，哪些算一轮，哪些算一步？

官方把答案放在 core/agent-loop：Agent 的 inbox 按下一轮和下一步两条队列
分流消息，循环本身只做调用模型、运行工具、重复这一件事，其余全部由边界
事件组织。本章复刻这套结构的教学版。

## 7.1 轮次与步骤

先建立两个术语的直觉。一个助手在工位上的协作是这样的：

- 你交给他一份任务，他开始干活。从他动手到把结果交回你，这一次完整的
  唤醒到完成叫一轮，官方 zh 文档写作轮次，对应事件日志里的 turn。
- 一轮内部，他反复看一眼资料、动一下手。每次这样的循环叫一步，官方
  zh 文档写作步骤，对应英文 step。
- 他干活时你又递来一张纸条。纸条分两种：写下一件事的，他先放进待办，
  等这轮干完再处理；写现在马上改这里的，他扫一眼就在下一步动作里体现。

把直觉翻成术语：

| 协作直觉 | Agent 术语 | 本章实现 |
|----------|-----------|----------|
| 一次唤醒到完成 | turn（轮次） | `turn/start` 与 `turn/end` 夹住的区间 |
| 看一眼资料 + 动一下手 | step（步骤） | 一次模型调用加工具执行 |
| 下一件事纸条 | followup | 进 next-turn 队列，轮次边界领取 |
| 马上改这里纸条 | steer | 进 next-step 队列，每个 step 开始前领取 |

轮次为什么重要，两个实际用途：

1. 边界即安全区。压缩（第 09 章）、持久化（第 08 章）这类动作只在轮次
   边界做，边界处没有半截的模型调用，历史是安静的。
2. 账单与审计的粒度。一次任务花了几个轮次、每轮几步，是成本分析的
   最小单位。官方事件日志里 turn/start、turn/end 一应俱全，正是为此。

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

    def claim_turn(self) -> Message | None:
        if self._next_turn:
            return self._next_turn.popleft()
        if self._next_step:
            return self._next_step.popleft()
        return None

    def claim_step(self) -> Message | None:
        if self._next_step:
            return self._next_step.popleft()
        return None
```

三个细节：

- `deque` 是标准库的双端队列，`popleft` 从头取，FIFO 顺序保证先到先
  处理。
- `claim_turn` 顺带领 `_next_step`：步骤级输入也可能开新轮。上一轮
  结束时恰好插队进来的 steer，就是下一轮的起点。官方文档写明轮次边界
  处同时领取 next-step 输入和一条排队提示词，步骤之间只领取 next-step
  输入，两个领取时机正是 followup 与 steer 语义差别的全部来源。
- 教学版的 `claim_step` 只领不检查唤醒。官方两条队列的投递都带唤醒
  语义，inject 是第三个别名，投递到 next-step 但不唤醒，留给练习 2。

## 7.3 Agent：常驻循环

Agent 持有 inbox、会话日志和请求 envelope，对外只暴露两个入口，
`followup` 投递常规问题，`steer` 投递中途引导：

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
            message = self._inbox.claim_turn()
            if message is None:
                break
            self._turn_no += 1
            self._session.append("turn/start", {"turn": self._turn_no})
            self._session.append("user/message", {"content": message.content})
            self._run_turn(tools, tools_by_name)
            self._session.append(
                "turn/end", {"turn": self._turn_no, "reason": "completed"}
            )
        return self._session
```

主循环的骨架一目了然：领取一条消息，开一轮，记录边界，跑完一轮，关一轮，
直到 inbox 清空。第 06 章的循环体整体搬进 `_run_turn`，只加一处：

```python
    def _run_turn(self, tools, tools_by_name) -> None:
        for _step in range(10):  # 安全阀：单轮最多 10 个 step
            # 每个 step 开始前领 step 级输入：steer 在这里插队生效
            if steer_message := self._inbox.claim_step():
                self._session.append(
                    "user/message",
                    {"content": steer_message.content, "steered": True},
                )

            messages = [
                Message(role="system", content=self._assembler.render(self._variables)),
                *self._session.derive_messages(),
            ]
            # ...请求模型、执行工具（与第 06 章相同）
            if not reply.tool_calls:
                return
```

steer 消息被记录成一条打了标记的 user 消息，`steered: True`。下一个 step
请求模型时它自然出现在历史里，模型当场看到引导，本轮行为立刻修正。
`steered` 标记进日志但不影响投影，纯粹给审计用。

教学版有两处简化，官方做法写在对照表里：官方 Agent 是常驻驱动器，空闲时
挂起等待唤醒，教学版改成有消息就同步跑完；官方的终止条件是一个体系，
completed、blocked、取消、错误各归各的，教学版只保留 completed 与
max_turns 安全阀。

## 7.4 跑一遍完整 demo

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

1. 历史延续。第 2 轮打印出的消息里有两条 assistant，第一条是第 1 轮的
   答案。派生视图自动包含全部历史，模型在第 2 轮确实记得上一轮发生了
   什么。
2. 边界清晰。14 条事件被分成两个由 turn/start 与 turn/end 夹住的完整
   块，这是轮次边界在日志里的样子，第 08 章与第 09 章会反复用到它。
3. 每轮两个 step。一次模型调用加工具执行就是一个 step，观察
   assistant/message 与 tool/call 的交替，一轮里交替几次就是几个 step。

## 本章小结

- `Inbox`：next-turn 与 next-step 两条队列，两个领取时机
- `Agent`：常驻循环，领取、开轮、轮内 step 循环、关轮
- `steer` 机制：step 级插队，带标记记录进日志
- turn（轮次）与 step（步骤）术语体系，以及轮次边界在日志中的形态

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/core/agent-loop/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/agent-loop/README.zh.md) | 术语 | 官方 zh 文档第 5 行即以轮次、步骤命名 turn 与 step，本章沿用 |
| 同上，第 58 行 | `Inbox` | 官方 `send()` 原语按 target × wakeup 路由，followup/steer/inject 是固定别名；followup 进 next-turn FIFO 并唤醒，steer 进 next-step inbox 并唤醒，inject 进 next-step 但不唤醒 |
| 同上，第 56 行 | `Agent` | 官方 ReactLoopAgent 与 inbox 均为包内部实现，对外只暴露 send 原语，教学版直接暴露类 |
| 同上，第 76 行 | `_run_turn` | 官方循环同样只做调用模型、运行工具、重复，其余全交给插件与事件 |
| 同上，第 105 行 | 会话日志 | 官方已接纳的消息与工具调用记录并在后续步骤发送，与本章投影机制一致 |

## 练习

1. **steer 实战。** 先 followup 一个算式问题，在 run 之前再 steer 一条
   用中文回答、不要用 LaTeX 公式，对比有与没有 steer 时模型输出的风格
   差异。
2. **inject 补全。** 仿照官方第三种原语 inject，给 Inbox 加一个投递到
   next-step 但不唤醒的入口，思考它适合什么场景。
3. **轮次推演。** 纸笔推演 demo 两个轮次的完整事件序列，标注每个 step
   的边界，与真实输出对比。
4. **max_turns 行为。** 把 max_turns 设为 1，投递两个 followup，观察
   第二条消息去哪了，思考真实框架该如何处理没处理完的消息。
