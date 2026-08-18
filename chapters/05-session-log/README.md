# 05｜会话日志

> 预计时间：60 分钟 ｜ 前置：完成第 02 章 ｜ 本章调用真实 DeepSeek 模型

第 02 章的 Agent 循环里，对话历史是一个朴素的 `list[Message]`，模型每轮看到
的，就是循环里直接改的那个列表。这个设计在第 02 章够用，但真实 Agent 里
很快会撞上三堵墙：

1. 不是所有历史都该给模型看。一次真实运行会产生大量过程记录：流式分片、
   生命周期边界、压缩动作。它们要留存，但发给模型只会浪费 token、制造噪音。
2. 一份历史要服务多个消费者。模型请求要读它、磁盘持久化要写它、界面要展示
   它、崩溃恢复要重放它。每个消费者各存一份，早晚不同步。
3. 历史一旦写入就不该再改。第 02 章的 list 谁都能 pop、insert，一个误操作
   就会毁掉整段对话的上下文。

官方 Harness 用同一个答案解决这三堵墙，这个答案也是本章的主角，事件溯源。
会话历史是一条只追加的事件日志，消息历史只是从日志派生出来的视图。官方
core/session 文档开篇就写明：Session 是 agent 全部交互历史的仅追加真源，
LLM 消息历史由它派生。

## 5.1 日志是事实，消息是投影

先想清楚日志和消息历史的区别。日志回答的问题是发生了什么：用户说了什么、
模型回了什么、哪个工具被调用了、结果是什么、这一轮何时开始何时结束。消息
历史回答的问题是模型该看到什么：把日志里对模型有用的部分挑出来，按协议
格式排好。

两者天然不同：日志要全，漏一条就丢一份事实；消息历史要精，多一条就浪费
token。事件溯源的设计把两者分开存储：

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

这个结构的三个好处正好对上三堵墙：日志全量保留过程记录，投影时只挑该给模型
的三种事件；持久化、界面、重放全部读同一份日志；日志只追加、写入即冻结，
天然不可篡改。官方文档把这层投影统称 surface：原始日志之上维护一个 surface
层，即产生消息事件的有序投影。下面动手实现。

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

- `id` 从 0 开始连续递增，5.6 节的重放校验靠它。
- `type` 是事件类型。本章用到 6 种：`turn/start`、`user/message`、
  `assistant/message`、`tool/call`、`tool/result`、`turn/end`。第 08 章
  持久化会接上更多种类。
- `ts` 是时间戳，记录事件发生的时刻。
- `data` 是事件内容，`frozen=True` 保证事件对象本身不可改。

## 5.3 Session.append：唯一的写入路径

`Session` 类只提供一个写入入口，`append`：

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

1. `_freeze_json(data)` 在写入前校验并冻结。校验拒绝函数、集合、循环引用等
   非纯 JSON 内容，日志要能持久化到磁盘，第 08 章会做，坏数据必须在写入时
   就失败，而不是几小时后落盘时才爆雷。冻结把 data 里的列表转成元组、字典
   转成只读形式，日志一旦写入就再无法修改。这是第 01 章 `frozen=True` 思想
   的升级版，从消息不可变到整段历史不可变。
2. `self._snapshot = None` 让外部读日志走 `events` 属性拿缓存快照，append 后
   缓存失效，下次读取重建，高频读取不被每次全量复制拖慢。
3. 通知订阅者。`subscribe` 挂在 Session 上的观察者实时收到新事件。官方的
   持久化插件正是这样工作的，官方文档写明插件订阅 session/event，在 flush
   时刷新。第 08 章我们自己也写一个这样的插件。

## 5.4 哪些事件进模型，哪些只进日志

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

三种事件投影成消息，其余一律 `None`，留在日志里，不进模型：

| 事件 | 投影结果 | 说明 |
|------|----------|------|
| `user/message` | `role="user"` | 人的话 |
| `assistant/message` | `role="assistant"` | 模型的话，可能带 tool_calls |
| `tool/result` | `role="tool"` | 工具结果，带 tool_call_id 对应 |
| `turn/start`、`tool/call`、`turn/end` | 不投影 | 过程记录，只进日志 |

`tool/call` 单独说一句：它记录模型请求调用工具这件事本身，含参数原文，但不
投影成消息。模型不需要被告知它自己请求过什么，`assistant/message` 里的
tool_calls 已经携带了这份信息。日志保留它是给审计和界面用的。这正是日志要
全、消息要精的典型体现。

## 5.5 循环日志化

第 02 章的循环直接操作 `history` 列表。日志化之后，循环的每一步都变成追加
一条事件：

```python
def run_agent(client, tools, system_prompt, user_prompt, max_turns=10) -> Session:
    tools_by_name = {tool.name: tool for tool in tools}
    session = Session()

    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": user_prompt})

    for turn in range(max_turns):
        # 模型看到的历史永远是日志的投影，不是日志本身
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
            # 执行工具，结果追加为 tool/result，错误同样回灌
    # ...
```

两个结构性变化：

1. 系统提示词不进日志。`messages` 的第一条 system 消息是循环现场拼的，不
   append 进 Session。它是给模型的规矩，不是对话里发生的事实。官方同样把
   system 与工具清单作为请求 envelope 的一部分单独管理，第 06 章展开。
2. `turn/start` 与 `turn/end` 夹住一轮。轮次是一次唤醒到完成的边界，这个
   边界在第 07 章与第 09 章里都会用到，压缩只发生在轮次边界，因为只有边界
   处历史是安静的。

## 5.6 重放：从日志重建一切

日志全量留存的最大红利是重放：拿到一份日志，就能重建会话的任何状态。
`from_log` 是重建入口：

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

校验 id 连续性不是洁癖。日志是唯一事实来源，缺一条，无论文件损坏还是手动
篡改，都必须响亮失败，而不是带着残缺历史继续跑、在几轮之后才莫名其妙地
出错。官方的校验更严，除了序号，还校验轮次闭合、工具调用与结果配对。

重放能力在官方手里有三处大用：崩溃恢复，从落盘日志重建会话继续跑；会话
fork，子 agent 以父对话的已完成前缀为一次性种子，第 14 章对照官方说明这一
机制；以及调试，回放一次运行看它每一步做了什么。本章 demo 演示最基础的
形态，同一份日志重建出完全相同的消息历史。

## 5.7 跑一遍完整 demo

```bash
uv run python chapters/05-session-log/src/demo.py
```

真实输出，模型回复内容每次不同，事件结构稳定：

```
=== ① 订阅者视角 ===
  [订阅者] 看到新事件 #0 user/message

=== ② 事件日志原文（唯一事实来源） ===
  #0  turn/start           {'turn': 1}
  #1  user/message         {'content': '1+2*3 等于几？'}
  #2  assistant/message    {'content': '', 'tool_calls': ({'id': 'call_00_...', 'name': 'calculator', 'arguments': '{"expression": "1+2*3"}'},)}
  #3  tool/call            {'call_id': 'call_00_...', 'name': 'calculator', 'arguments': '{"expression": "1+2*3"}'}
  #4  tool/result          {'call_id': 'call_00_...', 'content': '7.0', 'is_error': False}
  #5  assistant/message    {'content': '根据计算，**1 + 2 × 3 = 7**。\n\n这里需要注意运算顺序：先算乘法（2 × 3 = 6），再算加法（1 + 6 = 7）。', 'tool_calls': ()}
  #6  turn/end             {'turn': 1, 'reason': 'completed'}

=== ③ 派生消息：模型看到的历史（derive_messages 投影） ===
  [user] 1+2*3 等于几？
  [assistant → 请求工具] calculator({"expression": "1+2*3"})
  [tool → 结果] 7.0
  [assistant] 根据计算，**1 + 2 × 3 = 7**。

  这里需要注意运算顺序：先算乘法（2 × 3 = 6），再算加法（1 + 6 = 7）。

=== ④ 重放：同一份日志 → 新会话 → 完全相同的消息历史 ===
  重放派生 4 条消息，与原始派生一致: True
  ← 日志是唯一事实来源：持久化、界面、重放都从它派生
```

第 ② 节是事实，7 条事件，含 tool/call 这条不投影的过程记录；第 ③ 节是
投影，4 条消息，tool/call 不在其中；第 ④ 节证明投影只依赖日志，同一份
日志两次派生结果一致。事件 data 里的列表在冻结后变成元组，这是 5.3 节
冻结的痕迹，不可变性的可见证据。

## 本章小结

- `SessionEvent`：日志最小单位，id 连续、frozen
- `Session.append`：唯一写入路径，校验、冻结、快照失效、通知订阅者
- `derive_messages`：投影规则，三种事件进模型，其余只进日志
- 循环日志化：turn/start 到 turn/end 的完整事件序列
- `from_log`：重放入口，id 连续性校验、响亮失败

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/core/session/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/session/README.zh.md) | `Session` | 事件溯源定义在第 5 行，append 快照加冻结在第 39 行，events 缓存快照在第 43 行，与本章一致 |
| 同上，第 40-41 行 | `derive_messages` | 官方做增量投影，每个 surface 节点只投影一次，教学版每次全量。日志小时无差别，长会话是第 09 章压缩的伏笔 |
| 同上，第 11 行 | `subscribe` | 官方持久化插件订阅 session/event 在 flush 时落盘，第 08 章兑现 |
| [`packages/core/agent-loop/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/agent-loop/README.zh.md) | 循环日志化 | 官方第 105 行同样写明已接纳的消息与工具调用记录并在后续 step 发送，原始流分片仅写日志 |

## 练习

1. **投影实验。** 在 `_derive_event_message` 里把 `tool/call` 也投影成一条
   消息，role 自定，跑一遍 demo，观察模型行为是否变化。变了或不变，各自的
   原因是什么？
2. **篡改测试。** 拿到 `session.events` 快照后，尝试修改其中一条事件的
   `data` 字典，比如 `events[0].data["content"] = "篡改"`，观察冻结如何拦截。
   拦截发生在哪一层，是事件对象还是 data 内部？
3. **断点续跑。** 把 demo 跑一半的日志 `from_log` 重建，再继续追加一条
   `user/message`，观察新旧事件自然衔接。这是第 08 章持久化恢复的雏形，
   描述它和正式恢复的差距。
4. **设计取舍。** 为什么 system 提示词不进日志？它进了日志会带来什么
   麻烦，从压缩、fork、多模型三个角度各想一个。
