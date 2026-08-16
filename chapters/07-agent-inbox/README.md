# 07｜常驻的 Agent：轮次、收件箱与中途引导

> 预计时间：65 分钟 ｜ 前置：完成第 06 章 ｜ 本章调用真实 DeepSeek 模型

第 06 章的 `run_agent` 是一次性的：一个问题进去，一个结果出来，函数
返回后 Agent 就「死」了。真实的使用方式不是这样——一个 Agent 会话
里，用户会连续问好几个问题，每问一次，Agent 都要**接着之前的上下文**
继续工作；更微妙的是，Agent 正在干活时，用户可能突然插一句「停，换个
思路」。本章把一次性函数升级成**常驻的 Agent**，并回答两个问题：

1. 新消息在 Agent 忙碌时到达，该怎么排队、何时生效？
2. 一次 Agent 运行里，哪些算「一轮」（turn）、哪些算「一拍」（step）？

官方把答案放在 `core/agent-loop` 里：Agent 的收件箱（inbox）按
「下一轮」和「下一步」两条队列分流消息（文档第 58 行），循环本身只做
「调用模型、运行工具、重复」这一件事（第 76 行），其余全部由边界
事件组织。本章复刻这套结构的教学版。

## 7.1 原理：轮次、拍、收件箱

先建立三个术语的直觉。设想你和一个助手在工位上的协作：

- 你交给他一份任务，他开始干活。从他动手到把结果交回你，这**一次
  完整的「唤醒到完成」**叫一轮（turn）。
- 一轮内部，他可能要反复「看一眼资料（模型调用）、动一下手（工具
  执行）」——每次这样的循环叫一拍（step）。
- 他干活时你又递来一张纸条。纸条分两种：写「下一件事」的，他先放
  进待办（等这轮干完再处理）；写「现在马上改这里」的，他扫一眼就
  在下一步动作里体现。

把直觉翻成术语：

| 协作直觉 | Agent 术语 | 本章实现 |
|----------|-----------|----------|
| 一次唤醒到完成 | turn（轮次） | `turn/start` 与 `turn/end` 夹住的区间 |
| 看一眼资料 + 动一下手 | step（拍） | 一次模型调用 + 工具执行 |
| 「下一件事」纸条 | followup | 进 next-turn 队列，轮次边界领取 |
| 「马上改这里」纸条 | steer | 进 next-step 队列，每拍开始前领取 |

轮次为什么重要？两个实际用途：

1. **边界即安全区**。压缩（第 09 章）、持久化（第 08 章）这类动作
   只敢在轮次边界做——边界处没有半截的模型调用，历史是「安静」的。
2. **账单与审计的粒度**。一次任务花了几个轮次、每轮几拍，是成本
   分析的最小单位。官方的事件日志里 `turn/start`、`step/end` 一应
   俱全，正是为此。

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

值得注意的三个细节：

- **`deque`（双端队列）**：标准库里的高效队列，`popleft` 从头取。
  FIFO 顺序保证先到先处理。
- **`claim_turn` 会顺带领 `_next_step`**：步骤级输入也可能开新轮——
  上一轮结束时恰好插队进来的 steer，就是下一轮的起点。
- **两个领取时机分开**：`claim_turn` 在轮次边界调用（主循环里），
  `claim_step` 在每拍开始前调用（轮内循环里）。时机不同，正是
  followup 与 steer 语义差别的全部来源。

## 7.3 Agent：常驻循环

Agent 持有收件箱、会话日志和请求信封，对外只暴露两个入口——
`followup`（投递常规问题）和 `steer`（投递中途引导）：

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

主循环的骨架一目了然：**领取一条消息 → 开一轮 → 记录边界 → 跑完一轮 →
关一轮**，直到收件箱清空。第 06 章的循环体整体搬进 `_run_turn`，只加了
一处：

```python
    def _run_turn(self, tools, tools_by_name) -> None:
        for _step in range(10):  # 安全阀：单轮最多 10 拍
            # 每拍开始前领 step 级输入：steer 在这里插队生效
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

steer 消息被记录成一条**打了标记的 user 消息**（`steered: True`），
下一拍请求模型时它自然出现在历史里——模型当场看到引导，本轮行为
立刻修正。`steered` 标记进日志但不影响投影，纯粹给审计用。

教学版的两处简化，官方做法写在对照表里：

- 官方 Agent 是**常驻驱动器**：空闲时挂起等待唤醒（`send()` 原语带
  wakeup 语义），我们改成「有消息就同步跑完」；
- 官方的终止条件是一个体系（completed / blocked / 取消 / 错误），
  我们只保留 completed 与 max_turns 安全阀。

## 7.4 跑一遍完整 demo

```bash
uv run python chapters/07-agent-inbox/src/demo.py
```

真实输出（模型回复内容每次不同）：

```
=== 第 1 轮：问 1+2*3 ===
  [assistant] **1 + 2 × 3 = 7**

先乘除后加减：2 × 3 = 6，再加 1，得 7。

=== 第 2 轮：followup 再问 8/4（同一会话，历史延续） ===
  [assistant] 1 + 2 × 3 = 7（第一问的答案仍在历史里）
  [assistant] **8 ÷ 4 = 2**

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

1. **历史延续**：第 2 轮打印出的消息里有两条 assistant——第一条是
   第 1 轮的答案（派生视图自动包含全部历史），模型在第 2 轮确实
   「记得」上一轮发生了什么。
2. **边界清晰**：14 条事件被分成两个由 `turn/start…turn/end` 夹住的
   完整块——这就是「轮次边界」在日志里的样子，第 08/09 章会反复
   用到它。
3. **每轮 3 拍**：模型请求工具（拍 1）、再作答（拍 2）……观察
   assistant/message 与 tool/call 的交替，一次交替就是一拍。

## 7.5 本章小结：亲手写了什么

- `Inbox`：next-turn 与 next-step 两条队列、两个领取时机
- `Agent`：常驻循环——claim → turn/start → 轮内拍循环 → turn/end
- `steer` 机制：step 级插队，带标记记录
- turn / step 术语体系与轮次边界在日志中的形态

## 7.6 对照官方 DSH

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/core/agent-loop/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/agent-loop/README.zh.md) | `Inbox` | 官方 `send()` 原语按 target × wakeup 路由，followup/steer/inject 是固定别名（第 58 行）；官方还有 inject（不唤醒）第三种 |
| 同上（第 56 行） | `Agent` | 官方 ReactLoopAgent 与 inbox 均为包内部实现，对外只暴露 send 原语 |
| 同上（第 76 行） | `_run_turn` | 官方循环同样只做「调用模型、运行工具、重复」，其余全交给插件与事件 |
| 同上（第 105 行） | 会话日志 | 官方已接纳的消息与工具调用记录并在后续步骤发送，与本章投影机制一致 |

## 7.7 练习

1. **steer 实战**：先 followup 一个算式问题，在 run() 之前再 steer
   「用中文回答，不要用 LaTeX 公式」，对比有/无 steer 时模型输出的
   风格差异。
2. **inject 补全**：仿照官方第三种原语 inject，给 Inbox 加一个
   「投递但不唤醒」的队列，思考它适合什么场景（提示：预置上下文）。
3. **轮次推演**：纸笔推演 demo 两个轮次的完整事件序列（类型、顺序），
   标注每拍的边界，与输出对比。
4. **max_turns 行为**：把 max_turns 设为 1，投递两个 followup，观察
   第二条消息去哪了，思考真实框架该怎么处理「没处理完的消息」。
