# 05｜会话日志：让每一次运行都有迹可循

> 预计时间：60 分钟 ｜ 前置：完成第 02 章（第 03/04 章是插件系统的分支，本章回归 Agent 主线） ｜ 本章调用真实 DeepSeek 模型

第 02 章的 Agent 循环里，对话历史是一个朴素的 `list[Message]`——模型
每轮看到的，就是循环里直接改的那个列表。这个设计在第 02 章够用，但真实
Agent 里很快会撞上三堵墙：

1. **不是所有历史都该给模型看**。一次真实运行会产生大量「过程记录」：
   流式分片、生命周期边界（turn 开始/结束）、压缩动作……它们要留存，
   但发给模型只会浪费 token、制造噪音。
2. **一份历史要服务多个消费者**。模型请求要读它、磁盘持久化要写它、
   界面要展示它、崩溃恢复要重放它。每个消费者各存一份，早晚不同步。
3. **历史一旦写入就不该再改**。第 02 章的 list 谁都能 `pop`、`insert`，
   一个误操作就会毁掉整段对话的上下文。

官方 Harness 用同一个答案解决这三堵墙，也是本章的主角——**事件溯源**
（event sourcing）：会话历史是一条**只追加的事件日志**，消息历史只是
从日志**派生**出来的视图。官方 `core/session` 的文档第一句就是
「Session 是 agent 全部交互历史的仅追加真源，LLM 消息历史由它派生」。

## 5.1 原理：日志是事实，消息是投影

先想清楚「日志」和「消息历史」的区别。日志回答的问题是**发生了什么**：
用户说了什么、模型回了什么、哪个工具被调用了、结果是什么、这一轮何时
开始何时结束。消息历史回答的问题是**模型该看到什么**：把日志里对模型
有用的部分挑出来，按协议格式排好。

两者天然不同：日志要**全**（漏一条就丢一份事实），消息历史要**精**
（多一条就浪费 token）。事件溯源的设计把两者分开存储：

```
                     ┌─────────────────────────┐
   append ─────────▶ │  Session（事件日志）      │
   （唯一写入路径）    │  #0 turn/start          │
                     │  #1 user/message        │
                     │  #2 assistant/message   │
                     │  #3 tool/call           │───▶ derive_messages() ──▶ 发给模型
                     │  #4 tool/result         │        （投影）
                     │  #5 turn/end            │
                     └─────────────────────────┘
```

这个结构的三个好处正好对上三堵墙：

- 日志全量保留过程记录，**投影**（derive）时只挑该给模型的三种事件——
  第 1 堵墙解决；
- 持久化、界面、重放全部读同一份日志，谁也改不了谁——第 2 堵墙解决；
- 日志只追加、写入即冻结，天然不可篡改——第 3 堵墙解决。

官方把这三种会被投影的事件叫 **surface 层**（`core/session` 文档第 5 行：
「原始日志之上维护一个 surface 层——产生消息事件的有序投影」）。下面
动手实现。

## 5.2 SessionEvent：日志里的一条事件

事件是日志的最小单位：

```python
@dataclass(frozen=True)
class SessionEvent:
    id: int
    type: str
    ts: float
    data: dict[str, Any]
```

- `id`：从 0 开始连续递增——5.6 节的重放校验靠它。
- `type`：事件类型。本章用到 7 种：`turn/start`、`user/message`、
  `assistant/message`、`tool/call`、`tool/result`、`turn/end`，以及
  第 08 章持久化会接上的更多种类。
- `ts`：时间戳，记录事件发生的时刻。
- `data`：事件内容，`frozen=True` 保证事件对象本身不可改。

## 5.3 Session.append：唯一的写入路径

`Session` 类只提供一个写入入口——`append`：

```python
class Session:
    def __init__(self) -> None:
        self._log: list[SessionEvent] = []
        self._snapshot: list[SessionEvent] | None = None
        self._listeners: list[Any] = []

    def append(self, type: str, data: dict[str, Any]) -> SessionEvent:
        frozen_data = _freeze_json(data)
        event = SessionEvent(id=len(self._log), type=type, ts=_now(), data=frozen_data)
        self._log.append(event)
        self._snapshot = None
        for listener in list(self._listeners):
            listener(event)
        return event
```

三个动作，各对应一个设计意图：

1. **`_freeze_json(data)`**：写入前校验 + 深冻结。校验拒绝函数、集合、
   循环引用等非纯 JSON 内容——日志要能持久化到磁盘（第 08 章），坏数据
   必须在写入时就失败，而不是几小时后落盘时才爆雷。深冻结把 data 里的
   列表转成元组、字典转成只读形式，**日志一旦写入就再无法修改**。这是
   第 01 章 `frozen=True` 思想的升级版：从「消息不可变」到「整段历史
   不可变」。
2. **`self._snapshot = None`**：外部读日志走 `events` 属性拿缓存快照，
   append 后缓存失效，下次读取重建——高频读取不被每次全量复制拖慢。
3. **通知订阅者**：`subscribe` 挂在 Session 上的观察者实时收到新事件。
   官方的持久化插件正是这样工作的——「插件订阅 session/event，在
   session/flush 时刷新」（`core/session` 文档第 11 行）。第 08 章我们
   自己写一个这样的插件。

## 5.4 投影：哪些事件进模型，哪些只进日志

`derive_messages()` 是投影规则的实现：

```python
    def derive_messages(self) -> list[Message]:
        messages: list[Message] = []
        for event in self._log:
            message = _derive_event_message(event)
            if message is not None:
                messages.append(message)
        return messages


def _derive_event_message(event: SessionEvent) -> Message | None:
    if event.type == "user/message":
        return Message(role="user", content=event.data["content"])
    if event.type == "assistant/message":
        raw_calls = event.data.get("tool_calls") or []
        tool_calls = tuple(
            ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
            for c in raw_calls
        )
        if event.data.get("content") is None and not tool_calls:
            return None
        return Message(
            role="assistant",
            content=event.data.get("content"),
            tool_calls=tool_calls,
        )
    if event.type == "tool/result":
        return Message(
            role="tool",
            content=event.data["content"],
            tool_call_id=event.data["call_id"],
        )
    return None
```

三种事件投影成消息，其余一律 `None`（留在日志里，不进模型）：

| 事件 | 投影结果 | 说明 |
|------|----------|------|
| `user/message` | `role="user"` | 人的话 |
| `assistant/message` | `role="assistant"` | 模型的话，可能带 tool_calls |
| `tool/result` | `role="tool"` | 工具结果，带 tool_call_id 对应 |
| `turn/start`、`tool/call`、`turn/end` | 不投影 | 过程记录，只进日志 |

`tool/call` 值得单独说一句：它记录「模型请求调用工具」这件事本身（含
参数原文），但**不投影成消息**——模型不需要被告知它自己请求过什么，
`assistant/message` 里的 tool_calls 已经携带了这份信息。日志保留它是
给审计和界面用的。这正是「日志要全、消息要精」的典型体现。

## 5.5 循环日志化：从改列表到记事件

第 02 章的循环直接操作 `history` 列表。日志化之后，循环的每一拍都变成
「追加一条事件」：

```python
def run_agent(client, tools, system_prompt, user_prompt, max_turns=10) -> Session:
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
            # ...执行工具，结果追加为 tool/result（错误同样回灌）
    # ...
```

两个结构性变化：

1. **系统提示词不进日志**。注意 `messages` 的第一条 system 消息是循环
   现场拼的，不 append 进 Session——它是「给模型的规矩」，不是「对话
   里发生的事实」。官方同样把 system 与工具清单作为请求信封的一部分
   单独管理（第 06 章展开）。
2. **`turn/start` / `turn/end` 夹住一轮**。turn（轮次）是「一次唤醒到
   完成」的边界。这个边界在第 07 章（Agent 循环）、第 09 章（压缩）里
   都会用到——压缩只发生在轮次边界，因为只有边界处历史是「安静」的。

## 5.6 重放：从日志重建一切

日志全量留存的最大红利是**重放**：拿到一份日志，就能重建会话的任何
状态。`from_log` 是重建入口：

```python
    @classmethod
    def from_log(cls, events: list[SessionEvent]) -> "Session":
        session = cls()
        for index, event in enumerate(events):
            if event.id != index:
                raise ValueError(
                    f"重放失败：第 {index} 个事件 id 为 {event.id}（应为 {index}）"
                )
            session._log.append(event)
        return session
```

校验 id 连续性不是洁癖：日志是唯一事实来源，缺一条（文件损坏、手动
篡改）都必须**响亮失败**，而不是带着残缺历史继续跑、在几轮之后才
莫名其妙地出错。官方的校验更严——除了序号，还校验轮次闭合、工具调用
与结果配对（`core/session` 的 invariant 模块）。

重放能力在官方手里有三处大用：崩溃恢复（从落盘日志重建会话继续跑）、
会话 fork（第 14 章子 agent 继承父对话）、以及调试（回放一次运行看
它每一步做了什么）。本章 demo 演示最基础的形态：同一份日志重建出
完全相同的消息历史。

## 5.7 跑一遍完整 demo

```bash
uv run python chapters/05-session-log/src/demo.py
```

真实输出（模型回复内容每次不同，事件结构稳定）：

```
=== ① 订阅者视角 ===
  [订阅者] 看到新事件 #0 user/message

=== ② 事件日志原文（唯一事实来源） ===
  #0  turn/start           {'turn': 1}
  #1  user/message         {'content': '1+2*3 等于几？'}
  #2  assistant/message    {'content': '', 'tool_calls': ({...calculator...},)}
  #3  tool/call            {'call_id': '...', 'name': 'calculator', 'arguments': '{"expression": "1+2*3"}'}
  #4  tool/result          {'call_id': '...', 'content': '7.0', 'is_error': False}
  #5  assistant/message    {'content': '1+2×3 等于 **7**。...', 'tool_calls': ()}
  #6  turn/end             {'turn': 1, 'reason': 'completed'}

=== ③ 派生消息：模型看到的历史（derive_messages 投影） ===
  [user] 1+2*3 等于几？
  [assistant → 请求工具] calculator({"expression": "1+2*3"})
  [tool → 结果] 7.0
  [assistant] 1+2×3 等于 **7**。
  （根据四则运算的优先级规则……）

=== ④ 重放：同一份日志 → 新会话 → 完全相同的消息历史 ===
  重放派生 4 条消息，与原始派生一致: True
  ← 日志是唯一事实来源：持久化、界面、重放都从它派生
```

对照三节内容看输出：第 ② 节是**事实**（7 条事件，含 tool/call 这条
不投影的过程记录）；第 ③ 节是**投影**（4 条消息，tool/call 不在其中）；
第 ④ 节证明投影只依赖日志——同一份日志，两次派生，结果一致。另外
注意事件 data 里的列表在冻结后变成了元组（`(...)`）——那是 5.3 节
深冻结的痕迹，不可变性的可见证据。

## 5.8 本章小结：亲手写了什么

- `SessionEvent`：日志最小单位（id 连续、frozen）
- `Session.append`：唯一写入路径——校验、深冻结、快照失效、通知订阅者
- `derive_messages`：投影规则——三种事件进模型，其余只进日志
- 循环日志化：`turn/start` 到 `turn/end` 的完整事件序列
- `from_log`：重放入口——id 连续性校验、响亮失败

## 5.9 对照官方 DSH

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/core/session/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/session/README.zh.md) | `Session` | 事件溯源定义（第 5 行）、append 快照+冻结（第 39 行）、events 缓存快照（第 43 行）与本章一致 |
| 同上（第 40-41 行） | `derive_messages` | 官方做增量投影（每个 surface 节点只投影一次），教学版每次全量——日志小时无差别，长会话是第 09 章压缩的伏笔 |
| 同上（第 11 行） | `subscribe` | 官方持久化插件订阅 `session/event` 在 flush 时落盘——第 08 章兑现 |
| [`packages/core/agent-loop/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/agent-loop/README.zh.md) | 循环日志化 | 官方第 105 行同样「已接纳的消息与工具调用记录并在后续步骤发送，原始流分片仅写日志」 |

## 5.10 练习

1. **投影实验**：在 `_derive_event_message` 里把 `tool/call` 也投影成
   消息（比如 role 设为 "tool"），跑一遍 demo，观察模型行为是否变化，
   解释为什么会变或不变。
2. **篡改测试**：拿到 `session.events` 快照后，尝试修改其中一条事件的
   `data` 字典（如 `events[0].data["content"] = "篡改"`），观察深冻结
   如何拦截。
3. **断点续跑**：把 demo 跑一半的日志 `from_log` 重建，再继续追加一条
   `user/message`，观察新旧事件自然衔接——这是第 08 章持久化恢复的雏形。
4. **设计取舍**：为什么 system 提示词不进日志？设想它进了日志会带来
   什么麻烦（提示：压缩、fork、多模型）。
