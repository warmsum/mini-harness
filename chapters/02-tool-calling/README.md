# 02｜工具调用

> 预计时间：70 分钟 ｜ 前置：完成第 01 章 ｜ 本章调用真实 DeepSeek 模型

第 01 章结束时，客户端已经能和模型完成一次完整的提问回答。但一个只会说话
的模型不是 Agent。真正的 Agent 会做事：读文件、算数字、执行命令、搜索网络。
这一章给模型装上第一只手，让它学会调用工具。

先看一个事实，它是本章全部内容的起点：语言模型不擅长算术。问它 1+2*3 等于
几，它可能答对也可能答错，因为它不是在计算，而是在猜最像答案的文本。可靠
的算术需要一个真正的计算器。问题随之而来：模型怎样使用一个真实存在的计算
器？答案就是本章的主角，Function Calling，函数调用，也叫 Tool Calling。

本章实现一次完整的模型到工具再到模型的往返：模型不直接回答，而是发出一份
调用 calculator、参数为 1+2*3 的请求；我们的代码执行真正的计算，把结果 7
送回模型；模型基于 7 给出最终答案。这个往返是 Agent 循环的最小形态，后面
所有章节都在扩展它。

## 2.1 模型为什么会调用工具

### 两条输出通道

OpenAI 兼容协议里，模型回复一条消息时可以走两条通道之一：

1. 文字通道，`content` 字段里直接写回答，第 01 章用的就是它；
2. 工具调用通道，`tool_calls` 字段里声明要调用某个工具以及参数。

模型在训练阶段学会了这个约定：当它认为自己需要外部帮助时，比如算数、查
资料、读文件，输出工具调用；拿到工具结果之后，再走文字通道给最终答案。
这个认为完全由模型自己决定，我们的代码只负责告诉模型有哪些工具可用，以及
替它执行。

### 一次工具调用的协议长什么样

模型回复中的 `tool_calls` 字段长这样，这是真实请求返回的完整结构：

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_00_xxxx",
            "type": "function",
            "function": {
              "name": "calculator",
              "arguments": "{\"expression\": \"1+2*3\"}"
            }
          }
        ]
      }
    }
  ]
}
```

三个必须记住的细节：

1. `arguments` 是 JSON 字符串，不是对象。协议规定参数以文本传输，执行前
   必须 `json.loads` 解析。这是新手最常见的坑，直接拿字符串当字典用。
2. `id` 是本次调用的身份证。工具结果回灌时必须带上它，让模型知道这条结果
   是回答哪次调用的。
3. `content` 是 `null`。模型请求工具时通常不说话，文字通道空着。

### 完整往返的四个 step

```
第一个 step：把问题与工具说明书发给模型
第二个 step：模型不回答，返回 tool_calls 请求
第三个 step：执行工具，把结果作为新消息回灌，再问模型
第四个 step：模型基于结果走文字通道，给出最终答案
```

画成时序图：

```mermaid
sequenceDiagram
    participant M as 模型
    participant A as Agent 循环
    participant T as calculator

    A->>M: 发送问题 + 工具清单（tools）
    M->>A: content=null，tool_calls=[calculator("1+2*3")]
    A->>T: json.loads 参数后执行
    T->>A: 返回 "7.0"
    A->>M: 回灌 role="tool" 消息（带 tool_call_id）
    M->>A: content="1+2*3 = 7"
```

下面动手实现。本章代码在 `chapters/02-tool-calling/src/`，四个文件：

```
chapters/02-tool-calling/src/
├── client.py     # 第 01 章的客户端 + 工具支持
├── calculator.py # 本章新增：一个安全的计算器工具
├── agent.py      # 本章新增：工具调用循环
└── demo.py       # 跑一次完整往返
```

## 2.2 工具的说明书：JSON Schema

模型要正确使用工具，需要一份说明书：工具叫什么、干什么用、接受什么参数。
这份说明书用 JSON Schema 描述，一种用 JSON 描述数据结构的标准格式。先定义
`Tool` 数据类和第一个工具 `calculator`：

```python
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    execute: Callable[[dict[str, Any]], str]


calculator = Tool(
    name="calculator",
    description="计算一个四则运算表达式，支持 + - * / 与括号，例如 '1+2*3'",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式，例如 '1+2*(3-1)'",
            }
        },
        "required": ["expression"],
    },
    execute=_run_calculator,
)
```

逐段理解：

- `name`、`description`、`parameters` 是给模型看的。模型读它们决定什么时候
  该用这个工具、参数怎么填。说明书写得好不好，直接决定模型用得对不对，
  `description` 里那句例如 1+2*3 看似多余，实际能显著提高模型传参的准确率。
- `parameters` 是 JSON Schema，声明参数是一个对象，含一个必填的字符串字段
  `expression`。模型据此生成 `{"expression": "1+2*3"}` 这样的 JSON。
- `execute` 是给程序跑的。真正干活的是它，与说明书完全分离。

### 工具的肚子：一个不用 eval 的计算器

`execute` 内部怎么算表达式？新手的第一个念头是 `eval(expression)`，方便，
但极其危险：`eval` 会执行任意 Python 代码，而 `expression` 是模型生成的
字符串，属于不可信输入。模型一旦被诱导返回
`"__import__('os').system('rm -rf ...')"`，`eval` 会真的执行它。

这是贯穿全书的沙箱思想第一课：来自模型的一切都是不可信输入。手写一个递归
下降解析器，只认数字和四则运算符，其余字符一律报错：

```python
def _evaluate(source: str) -> float:
    tokens = _tokenize(source)      # "1+2*(3-1)" → ["1","+","2","*","(","3","-","1",")"]
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        token = tokens[position]
        position += 1
        return token

    def parse_expression() -> float:      # 加减层：优先级最低
        value = parse_term()
        while peek() in ("+", "-"):
            operator = take()
            right = parse_term()
            value = value + right if operator == "+" else value - right
        return value

    def parse_term() -> float:            # 乘除层：优先级高于加减
        value = parse_factor()
        while peek() in ("*", "/"):
            operator = take()
            right = parse_factor()
            if operator == "*":
                value *= right
            else:
                if right == 0:
                    raise ValueError("除数为零")
                value /= right
        return value

    def parse_factor() -> float:          # 最内层：数字、括号、负号
        token = take()
        if token == "(":
            value = parse_expression()
            if take() != ")":
                raise ValueError("缺少右括号")
            return value
        if token == "-":
            return -parse_factor()
        return float(token)

    result = parse_expression()
    if position != len(tokens):
        raise ValueError(f"表达式在 {tokens[position]!r} 处意外结束")
    return result
```

这里体现了一个经典的编程思想，递归下降：每个语法层级一个函数，`expression`
调 `term`，`term` 调 `factor`，`factor` 遇到括号又回头调 `expression`。层级
的嵌套顺序本身就是运算符优先级，乘除在更深的层先算，加减在外层后算。
`1+2*3` 因此天然等于 `1+(2*3)`。

`_tokenize` 负责词法分析，把字符串切成有意义的词元：

```python
def _tokenize(source: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
        elif char in "+-*/()":
            tokens.append(char)
            index += 1
        elif char.isdigit() or char == ".":
            number = ""
            while index < len(source) and (source[index].isdigit() or source[index] == "."):
                number += source[index]
                index += 1
            tokens.append(number)
        else:
            raise ValueError(f"非法字符: {char!r}")
    return tokens
```

任何不在这三类里的字符，字母、引号、下划线，直接抛错。`eval` 能执行的
危险代码在这里连词法分析都过不了。完整的 `calculator.py` 见源码目录。

## 2.3 消息模型升级

第 01 章的 `Message` 只有 `role` 和 `content`。工具调用要求消息能表达更多
信息，升级如下：

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON 字符串，执行前要解析


@dataclass(frozen=True)
class Message:
    role: str
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
```

两个扩展点的作用：

- `tool_calls` 挂在 `assistant` 消息上，表示这条回复里模型请求调用这些工具。
  用 `tuple` 而不是 `list`，配合 `frozen=True` 的不可变性承诺，tuple 自身
  不可变。
- `tool_call_id` 挂在 `role="tool"` 的消息上，与 `ToolCall.id` 一一对应。
  协议规定工具结果回灌时必须标明它是回答哪次调用的，模型一轮可能同时请求
  多个工具，没有 id 就对不上号。

于是对话历史里出现了第四种角色 `tool`：

| 角色 | 谁说的话 | 何时出现 |
|------|----------|----------|
| `system` | 系统 | 历史最前面，立规矩 |
| `user` | 人 | 提问 |
| `assistant` | 模型 | 回答，或携带 tool_calls 请求工具 |
| `tool` | 工具 | 工具的执行结果，回灌给模型 |

`tool` 消息不进入人类视角的对话，它是给模型看的工作记录。官方 Harness 内部
同样把工具结果作为消息回灌，官方文档写明已接纳的 user 消息、assistant 消息、
工具调用与结果都会记录，并在后续 step 中发送。

## 2.4 客户端升级

请求侧需要把工具清单发给模型，协议里叫 `tools` 字段；响应侧需要把
`tool_calls` 解析成 `ToolCall` 对象。第 01 章的 `chat()` 升级如下：

```python
class DeepSeekClient:
    # 第 01 章部分略

    def chat(self, messages: list[Message], tools: list[Tool] | None = None) -> Message:
        payload = {
            "model": self.MODEL,
            "messages": [self._wire_message(m) for m in messages],
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        choice = data["choices"][0]
        raw_message = choice["message"]
        tool_calls: list[ToolCall] = []
        for raw_call in raw_message.get("tool_calls") or []:
            tool_calls.append(
                ToolCall(
                    id=raw_call["id"],
                    name=raw_call["function"]["name"],
                    arguments=raw_call["function"]["arguments"],
                )
            )
        return Message(
            role="assistant",
            content=raw_message.get("content"),
            tool_calls=tuple(tool_calls),
        )
```

三处与第 01 章的差异：

- 返回值从 `str` 变成 `Message`。模型回复现在可能有调用无文字，只返回字符串
  会丢掉信息。从这里开始，客户端始终返回完整的 `Message`。
- `tools` 清单里每个工具包一层 `{"type": "function", "function": {...}}`，
  这是协议要求的嵌套结构，`type` 固定为 `"function"`。
- `raw_message.get("tool_calls") or []` 是兜底，普通文字回复里没有这个字段，
  用 `get` 加空列表避免 KeyError。

`_wire_message` 负责反向转换，内部 `Message` 转协议 dict，其中 `role="tool"`
的消息必须带上 `tool_call_id`：

```python
    @staticmethod
    def _wire_message(m: Message) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": m.role}
        if m.content is not None:
            wire["content"] = m.content
        if m.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in m.tool_calls
            ]
        if m.tool_call_id is not None:
            wire["tool_call_id"] = m.tool_call_id
        return wire
```

## 2.5 Agent 循环：把四个 step 串起来

有了能请求工具的模型、能执行工具的代码，剩下的就是循环本身，本章的灵魂：

```python
def run_agent(
    client: DeepSeekClient,
    tools: list[Tool],
    system_prompt: str,
    user_prompt: str,
    max_turns: int = 10,
) -> list[Message]:
    tools_by_name = {tool.name: tool for tool in tools}
    history: list[Message] = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_prompt),
    ]

    for turn in range(max_turns):
        reply = client.chat(history, tools)
        history.append(reply)

        if not reply.tool_calls:
            return history

        for call in reply.tool_calls:
            tool = tools_by_name.get(call.name)
            if tool is None:
                result = f"Error: 模型请求了未注册的工具 {call.name!r}"
            else:
                try:
                    args = json.loads(call.arguments)
                    result = tool.execute(args)
                except Exception as error:
                    result = f"工具执行出错: {error}"
            history.append(
                Message(role="tool", content=result, tool_call_id=call.id)
            )

    raise RuntimeError(f"Agent 在 {max_turns} 轮内没有结束")
```

循环里有三个关键决策：

**决策一：终止条件。** 循环什么时候停？只有一种自然终点，模型不再请求工具，
也就是 `reply.tool_calls` 为空。`max_turns` 是安全阀：模型可能陷入反复请求
同一个工具的死循环，比如参数一直填错，没有上限程序就会永远转下去。官方
Harness 对失控轮次的治理更精细，第 07 章展开。

**决策二：错误回灌而不是中断。** 工具执行失败时，比如参数解析失败、除数为
零，程序不崩溃，而是把错误文本作为工具结果回灌。模型读到工具执行出错：
除数为零，下一轮会自己换参数重试。这模拟了人类遇到错误时的行为，Agent 的
健壮性来自让模型看见错误。

**决策三：一轮可多工具。** `reply.tool_calls` 是列表而非单个值，模型一轮可以
同时请求多个工具，逐个执行、逐个回灌。教学版串行执行，官方支持按并发安全
性并行调度，第 05 章展开。

## 2.6 跑一遍完整 demo

```bash
uv run python chapters/02-tool-calling/src/demo.py
```

真实输出，模型行为每次略有差异，工具调用这一步是稳定的：

```
=== 完整对话历史 ===

[system]
你是一个数学助手。遇到算式时先调用 calculator 工具计算，再基于计算结果回答。

[user]
1+2*3 等于几？

[assistant → 请求工具] calculator({"expression": "1+2*3"})

[tool → 结果 #call_00_jB51DjtgejamjcVcM6wq4888] 7.0

[assistant]
根据运算优先级（先乘除后加减）：

**1 + 2 × 3 = 1 + 6 = 7**

答案是 **7**。
```

对照 2.1 的四个 step：模型请求了工具，我们执行并回灌，模型基于 7.0 给出
最终答案。模型最后的回答里主动展示了运算过程，它读懂了工具结果，并把它
组织成人话。

### 错误路径：模型怎样面对失败

把 demo 里的 `user_prompt` 改成帮我算 1/0，再跑一次。这是决策二的实战
检验，工具执行抛错，错误被回灌，接下来发生什么：

```
[assistant → 请求工具] ['calculator({"expression": "1/0"})']
[tool → 结果] 工具执行出错: 除数为零
[assistant] 关于 1/0 的计算结果如下：

1/0 在数学中是未定义的（无意义），无法计算。

为什么？
- 任何数除以 0 都没有定义：没有任何数乘以 0 能等于 1
- 从极限角度看，1/0 两侧分别趋向正负无穷，不收敛于同一个值

计算工具也正确地拒绝了这一操作，返回了错误。
```

模型没有崩溃、没有放弃、也没有重复调用同一个会失败的工具。它读懂了错误
信息，把除数为零转化为数学知识组织成回答，甚至反过来肯定工具拒绝得对。
Agent 的健壮性来自这个闭环：错误被诚实地告诉模型，模型用它修正行为。这也
解释了为什么 2.5 节里工具异常要转成文本回灌而不是向上抛：抛掉异常，模型就
失去了自我修正的机会。

## 本章小结

- `Tool` 与 `calculator`：工具等于给模型看的说明书加给程序跑的执行器；
  计算器用递归下降解析器实现，拒绝 `eval`
- `ToolCall` 与扩展后的 `Message`：工具请求、`tool` 角色回灌、
  `tool_call_id` 对应关系
- `DeepSeekClient.chat()` 升级：注入 `tools` 清单、解析 `tool_calls`、
  返回完整 `Message`
- `run_agent()`：四 step 循环，终止条件、错误回灌、多工具执行三个关键决策

## 对照官方

| 官方代码 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/core/agent-loop/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/agent-loop/README.zh.md) | `run_agent()` | 官方循环同样把工具调用与结果记录并回灌，第 105 行；官方循环是流式加多 step 的完整版，第 07 章对齐 |
| [`packages/core/tools/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/tools/README.zh.md) | `Tool` | 官方 `ToolDefinition` 注册进 `ctx.tools` 注册表，执行走 pre-execute 到 execute 到 post-execute 流水线，第 5 行，第 05 章对齐 |
| [`packages/llm/llm/src/assembler.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/llm/llm/src/assembler.ts) | `chat()` 的解析 | 官方组装器在流式模式下逐分片拼装 tool-call 块；本章用非流式 `chat()` 拿完整 tool_calls，流式工具分片见练习 4 |

## 练习

1. **让模型面对一次失败。** 把 `user_prompt` 改成帮我算 1/0，观察模型与循环
   如何协作处理除数为零。模型收到错误后做了什么？它放弃了吗？它重复调用
   同一个工具了吗？把观察到的行为记录下来。
2. **加一个工具。** 仿照 `calculator` 定义一个 `datetime` 工具，返回当前时间，
   无参数。把现在几点这个问题交给 Agent，观察模型何时选择调用它而不是直接
   回答。如果模型直接回答了，想想说明书的哪部分没写清楚。
3. **验证说明书的力量。** 把 `calculator.description` 里的示例句删掉，反复问
   几个算式，对比模型传参格式的准确率变化。示例句为什么能提高准确率？给出
   你的解释。
4. **流式工具分片。** 官方在流式模式下，`tool_calls` 是逐分片到达的，
   `delta.tool_calls` 里名字和参数分开推送。扩展第 01 章的 `stream()`，把
   `delta.tool_calls` 拼装成完整的 `ToolCall` 列表，再与官方 `assembler.ts`
   的实现对照，找出官方的处理和你自己的差异。
