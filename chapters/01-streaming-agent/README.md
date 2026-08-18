# 01｜流式输出与消息组装

> 预计时间：60 分钟 ｜ 前置要求：会运行 Python 文件、知道 API Key 是什么 ｜ 本章调用真实 DeepSeek 模型

你在 ChatGPT 或 DeepSeek 网页版里问过问题。回车之后，回答是一个字一个字
长出来的，往往第一句还没看完，后面的内容已经在往外冒。这个体验背后是一
个很基础也很关键的机制，流式输出。

网页聊天把文字显示给人看，Agent 的工作方式不同。它要把每一轮对话存进历
史，带着历史反复调用模型，直到任务完成。于是同一个流式输出被拆成了两件
事：新生成的内容要立刻展示出来，而进入历史的必须是一条完整、稳定的消息。
这个分寸就是本章要实现的东西。

读完这一章，你会写一个能调用 DeepSeek 模型、支持流式输出、并把分片组装成
完整消息的 Python 客户端。这个客户端是全书的地基，从第 02 章开始，模型会
在它之上学会调用工具。

## 认识要调用的接口

写代码之前，先花几分钟把模型服务这件事本身搞清楚，后面的章节都在和它打
交道。

### API 与 OpenAI 兼容接口

模型跑在服务商的服务器上，程序要跟它通信，需要一个约定好的对接口：往某个
网址发送一段特定格式的数据，服务器处理完返回一段特定格式的数据。各家模型
厂商的对接口高度一致，因为大家默认采用 OpenAI 当年定下的规范，这套规范就
叫 OpenAI 兼容接口。学会一种调用方式，换厂商只需要改网址和 Key，代码几乎
不用动。DeepSeek 完全兼容这套规范，接口地址是 `https://api.deepseek.com`。

### 一次请求长什么样

调用模型等于向 `/chat/completions` 发送一个 HTTP POST 请求，请求体是 JSON，
核心字段有三个：

```json
{
  "model": "deepseek-chat",
  "messages": [
    { "role": "system", "content": "你是一个简洁的助手。" },
    { "role": "user", "content": "什么是流式输出？" }
  ],
  "stream": false
}
```

- `model` 指定用哪个模型，`deepseek-chat` 是 DeepSeek 的通用对话模型。
- `messages` 是对话历史，一个消息数组。模型对世界的全部了解都来自这个数组，
  模型没有记忆，给它什么它看什么。
- `stream` 为 false 表示全部想完再返回，为 true 表示边生成边返回。

### 三种角色

`messages` 里每条消息都有一个 `role`，三种角色各有分工：

| 角色 | 谁说的话 | 作用 |
|------|----------|------|
| `system` | 系统 | 放在最前面，给模型立规矩 |
| `user` | 用户 | 人提出的问题和要求 |
| `assistant` | 模型 | 模型自己的回答 |

`system` 存在的意义是给模型设定行为。模型的默认行为是热心回答一切，当需要
它扮演特定角色时，把要求写进 `system` 最有效。官方 Harness 的系统提示词就
承担这个职责，第 06 章会专门讲它。

### 一次响应长什么样

非流式请求的响应体同样是 JSON：

```json
{
  "choices": [
    {
      "message": { "role": "assistant", "content": "流式输出是……" }
    }
  ]
}
```

`choices` 是候选回答的数组，一般只有一项。`choices[0].message.content` 是模型
的完整回答文本，这个取值路径贯穿全书，第 02 章的工具调用会在这个 message
里多出一个 `tool_calls` 字段。

## 环境准备

需要三样东西，五分钟能搞定。

1. **Python 3.11 及以上**，终端运行 `python --version` 确认。
2. **uv**，一个用 Rust 写的 Python 包管理器，终端运行 `uv --version` 确认，
   没有就去 [astral.sh/uv](https://astral.sh/uv) 按说明安装。项目依赖写在
   根目录 `pyproject.toml` 里，`uv run python 某文件.py` 会自动安装缺失的包。
3. **DeepSeek API Key**，到 DeepSeek 开放平台申请。然后在项目根目录创建
   `.env` 文件，根目录已有 `.env.example` 模板，写入一行：

   ```bash
   DEEPSEEK_API_KEY=sk-你的key
   ```

   `.env` 在 `.gitignore` 的忽略列表里，永远不会提交到 GitHub。

## 先跑起来

```bash
uv run python chapters/01-streaming-agent/src/demo.py
```

三次调用，每次都是真实模型回答：

```
============================================================
演示 1：非流式调用（一次拿回完整回答）
============================================================
完整回答：流式输出是边生成边传输数据，无需等待全部完成后才显示，像打字机一样逐字呈现结果。

============================================================
演示 2：流式调用（边生成边显示）
============================================================
逐分片输出：流式输出是指数据在生成过程中逐段实时传输给用户，而非等待全部完成后一次性返回。

============================================================
演示 3：流式 + 组装成完整消息（Agent 的标准做法）
============================================================
进入历史的消息：role='assistant', 长度=45 字
消息内容：流式输出是指数据或内容在生成过程中分块、连续地传送给接收方，而非等待全部完成后一次性输出。
```

回答的文字由真实模型生成，每次略有不同。演示 2 在终端上的观感是一段一段冒
出来的打字机效果，这就是流式输出的意义，用户不用盯着空白屏幕干等。

本章代码都在 `chapters/01-streaming-agent/src/` 里，共两个文件：

```
chapters/01-streaming-agent/src/
├── client.py   # 本章主角：从零实现的客户端
└── demo.py     # 把 client.py 跑起来看效果
```

下面把它拆开，一步步自己写一遍。

## 1.1 把 API Key 读进来

调用模型前先拿到 Key。Key 不能硬编码在代码里，否则一开源就泄露，标准做法
是放进环境变量，代码只在运行时读取。本地学习时最方便的形式是 `.env` 文件。
`load_api_key()` 按环境变量优先、`.env` 兜底的顺序读取：

```python
import os
from pathlib import Path


def load_api_key() -> str:
    # 第一步：环境变量（部署到服务器时的标准做法）
    from_env = os.getenv("DEEPSEEK_API_KEY")
    if from_env:
        return from_env

    # 第二步：项目根目录的 .env 文件
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value

    raise RuntimeError("找不到 DEEPSEEK_API_KEY：请参考 .env.example 创建 .env")
```

逐行看：

- `os.getenv("DEEPSEEK_API_KEY")` 读环境变量，没有就返回 `None`。
- `Path(__file__).resolve().parents[3]` 是定位 `.env` 的关键。`__file__` 是
  这段代码自己的文件路径，文件位于 `chapters/01-streaming-agent/src/`，
  `parents[0]` 是 `src/`，`parents[1]` 是 `01-streaming-agent/`，`parents[2]`
  是 `chapters/`，`parents[3]` 才是项目根目录。`resolve()` 把相对路径变成
  绝对路径，无论从哪个目录启动程序都能找到 `.env`。
- 逐行扫描 `.env`，找到 `DEEPSEEK_API_KEY=` 开头的行，用 `split("=", 1)` 取
  等号后面的值，`1` 表示只切第一刀，Key 里即使有等号也不会被切坏。
- `strip().strip('"').strip("'")` 去掉首尾空格和可能的引号。
- 最后 `raise RuntimeError(...)`：Key 缺失时立刻失败并说清原因。坏状态要在
  最靠近它的地方响亮地失败，而不是带着一个空 Key 继续跑，直到发请求时才
  报一个莫名其妙的 401。

## 1.2 先定义一条消息

在写客户端之前，先定义贯穿全书的数据结构，一条对话消息。Python 的
`dataclass` 是定义这类数据类的标准工具，它省掉手写 `__init__` 的样板代码。
这里给它加一个关键修饰，`frozen=True`：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    role: str     # "system"（规矩）/"user"（人）/"assistant"（模型）
    content: str  # 消息正文
```

`frozen=True` 做了什么？`dataclass` 默认生成的 `__init__` 用 `self.role = role`
这样的方式给属性赋值，而 `frozen=True` 会额外生成一个 `__setattr__` 方法，
拒绝任何后续的属性写入。于是：

```python
m = Message(role="user", content="你好")
m.content = "篡改"   # 抛 FrozenInstanceError
```

Agent 的消息必须不可变。对话历史会被反复读取，每一轮都要完整地发给模型，
压缩、持久化、界面展示都要读它，任何一处代码悄悄改动了历史内容，后续所有
行为都会跟着错，而且极难排查。把禁止修改变成语言层面的约束，这类 bug 从
根源上被消灭。官方 Harness 的消息模型同样会冻结每条消息，第 05 章的事件
日志还会再次用到这个思想。

## 1.3 第一次调用：一次拿回完整回答

最朴素的调用方式：把历史发过去，等模型全部想完，一次性拿回完整回答。先把
客户端类的骨架和 `chat()` 写出来：

```python
import httpx


class DeepSeekClient:
    BASE_URL = "https://api.deepseek.com"
    MODEL = "deepseek-chat"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or load_api_key()

    def chat(self, messages: list[Message]) -> str:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.MODEL,
                    "messages": [{"role": m.role, "content": m.content} for m in messages],
                    "stream": False,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
```

逐段理解：

- `httpx` 是 Python 生态主流的 HTTP 客户端库，接口风格与经典库 `requests`
  一致，同时支持同步和异步两种用法，本章两节会各用一次。`with httpx.Client(...)`
  保证请求结束后连接被正确关闭。
- `Authorization` 头是 `Bearer <key>`，OpenAI 兼容接口的统一认证格式，
  DeepSeek 服务器读到它就知道你是谁、有没有额度。
- `Content-Type` 头告诉服务器请求体是 JSON 格式。
- `json={...}` 是 httpx 的便捷参数，自动把字典序列化成 JSON 并设置好格式头。
  请求体的三个字段正是前面讲的 `model`、`messages`、`stream`。
- `stream` 为 `False`，表示全部想完再一次性返回。
- `raise_for_status()` 在 HTTP 状态码非 2xx 时抛出带状态码的异常。401 表示
  Key 错误，429 表示请求过频，502 表示服务端问题。不检查状态码是新手最常见
  的坑，请求失败时直接往下解析，会得到一个莫名其妙的 KeyError。
- 返回路径 `data["choices"][0]["message"]["content"]` 对应前面的响应结构，
  逐层取到回答文本。

调用它只有一行：

```python
client = DeepSeekClient()
answer = client.chat(HISTORY)   # HISTORY 是一个 list[Message]
```

运行效果（演示 1）：

```
完整回答：流式输出是边生成边传输数据，无需等待全部完成后才显示，像打字机一样逐字呈现结果。
```

到这里，我们已经能和模型完成一次完整的提问回答。注意这行字是等模型全部生成
完才出现的，如果回答很长，用户会盯着空白屏幕等上几十秒。下一节解决这个
问题。

## 1.4 流式调用：边生成边显示

模型生成文字是逐字算出来的，一段几百字的回答可能要花 20 秒以上。非流式
模式下，这 20 秒里用户什么都看不到。网页版聊天没有这个问题，答案一个字
一个字往外蹦，因为网页版用的是流式接口。

流式接口的约定很简单：请求里把 `stream` 设为 `true`，服务器就不再一次性
返回完整回答，而是生成一小段就立刻推送一小段，直到全部完成。这些连续推送
的小片段称为 chunk，中文叫分片。模型说 500 个字，客户端可能收到几十个
chunk，每个 chunk 只包含几个字。

服务器持续推送数据用的是什么协议？答案是 SSE，Server-Sent Events，服务器
推送事件。普通的 HTTP 响应是一问一答，服务器把完整响应体发完就关闭连接。
SSE 的响应不同：服务器发完响应头后保持连接不关，之后持续不断地写入一条条
格式如下的数据：

```
data: {"choices":[{"delta":{"content":"流式"}}]}

data: {"choices":[{"delta":{"content":"输出"}}]}

data: [DONE]
```

三条规律：

1. 每条推送以 `data: ` 开头，后面跟一段文本，空行分隔。
2. 内容是增量而不是全量，每块只带新生成的一小段文字，所以取值路径是
   `choices[0].delta.content`，与 1.3 节的 `message.content` 只差一个字段名。
3. 结束信号是固定的一行 `data: [DONE]`，收到它表示模型说完了。

`httpx-sse` 库完成协议解析，连接管理、按空行切分事件、剥离 `data: ` 前缀，
我们拿到的就是干净的 `event.data` 字符串。

流式连接会挂着等数据，用同步代码处理它会卡死整个程序，因此本节引入两个新
的 Python 概念。这里先建立直觉，后面章节会反复用到。

async / await 是异步。同步程序的执行流是一条直线：调用函数，等它返回，继续。
网络等待期间，程序整个停住。异步程序把等待和执行分开，遇到 `await` 时先
挂起当前任务，事件循环去处理其他任务，数据到了再回来继续。本书不要求掌握
事件循环的实现细节，只要记住写法：异步函数用 `async def` 定义，调用时用
`await`，运行入口用 `asyncio.run(...)`。

yield 是生成器。函数里出现 `yield`，它就不再是普通函数，而是生成器：执行到
`yield` 时把值交给调用方，然后暂停在原地，调用方下一次迭代时从暂停处继续。
调用方拿到一个值可以立刻处理，比如打印到屏幕，而生成器继续等下一个 chunk，
这正是流式消费的天然形态。

下面是 `stream()` 的完整实现：

```python
import json

import httpx
from httpx_sse import aconnect_sse


class DeepSeekClient:
    # 上一节的字段和 chat() 略

    async def stream(self, messages: list[Message]):
        async with httpx.AsyncClient(timeout=60) as client:
            async with aconnect_sse(
                client,
                "POST",
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.MODEL,
                    "messages": [{"role": m.role, "content": m.content} for m in messages],
                    "stream": True,
                },
            ) as event_source:
                async for event in event_source.aiter_sse():
                    if event.data == "[DONE]":
                        break
                    payload = json.loads(event.data)
                    delta = payload["choices"][0].get("delta", {})
                    piece = delta.get("content")
                    if piece:
                        yield piece
```

逐段理解：

- `async with httpx.AsyncClient(...)` 是异步版 HTTP 客户端。`async with` 与
  普通 `with` 作用相同，用完自动关闭，区别是进出块时可以 `await` 等待。
- `aconnect_sse(...)` 是 httpx-sse 提供的异步 SSE 连接入口。第一个参数是
  http 客户端，然后是请求方法和地址，后面与 `chat()` 一样传 `headers` 和
  `json`，注意 `stream` 为 `True`。
- `async for event in event_source.aiter_sse()` 每收到一条 SSE 事件迭代一次，
  `event.data` 就是去掉 `data: ` 前缀后的文本。
- `if event.data == "[DONE]": break` 是 DeepSeek 的结束标记，收到就退出循环。
- `payload["choices"][0].get("delta", {})` 用 `get` 而不是下标，个别事件里
  可能没有 `delta` 字段，比如只带用量统计的事件，用 `get` 安全地给空字典。
- `if piece: yield piece` 只产出非空文本。有些 delta 的 `content` 是 `None`，
  比如流刚开始时的元信息，跳过它们。

调用方用 `async for` 消费生成器，配合 `flush=True` 让文字立刻上屏：

```python
async for piece in client.stream(HISTORY):
    print(piece, end="", flush=True)
```

`flush=True` 不能省。Python 的 `print` 默认带缓冲，不刷新的话文字会攒在内存
里，终端上看不到逐字蹦出的效果。

## 1.5 组装：历史只存完整消息

流式解决了看得快，同时制造了一个新问题。两种处理分片的方式，比较一下
后果：

- 每收到一个 chunk 就往历史里塞一条消息。下一轮请求将带着几十条碎消息发给
  模型，token 浪费还在其次，模型看到的历史会异常混乱。
- 流在中途断掉，比如网络抖动或用户取消。如果历史里已经塞了半句话，这条半个
  回答会永远留在历史里，后续每一轮都带着它。

官方 Harness 对这个问题给出的答案是 BlockAssembler，块组装器。流式分片一边
实时展示，一边被组装器暂存；流正常结束后，组装器产出一条完整消息，这条消息
才被写进历史。官方把这个边界守得比我们更严，连每个块的结束都有专门的
`block-end` 事件标记。我们先实现简化版：

```python
class DeepSeekClient:
    # 前两节略

    async def stream_message(self, messages: list[Message]) -> Message:
        pieces: list[str] = []
        async for piece in self.stream(messages):
            pieces.append(piece)
        return Message(role="assistant", content="".join(pieces))
```

- `pieces` 列表暂存所有分片，`"".join(pieces)` 把它们按顺序拼成完整文本。
- 返回的是 `Message` 实例，`frozen=True` 保证这条消息从此不可篡改。
- 流中途抛异常时，函数直接以异常结束，半成品消息根本不会产生，这就是历史只
  存完整消息的落地方式。

三行核心逻辑，但它是全书最重要的习惯之一。从第 02 章起，模型开始调用工具，
历史里会出现工具调用、工具结果等更多种类的消息，而流式展示、完整入史的规矩
自始至终不变。

运行效果（演示 3）：

```
进入历史的消息：role='assistant', 长度=45 字
消息内容：流式输出是指数据或内容在生成过程中分块、连续地传送给接收方，而非等待全部完成后一次性输出。
```

想亲眼验证 `frozen` 的约束，打开 `demo.py`，把最后一段被注释掉的
`message.content = "篡改"` 恢复，再跑一次，程序会抛出 `FrozenInstanceError`。

## 跑一遍完整 demo

本章代码共两个文件，全部自包含，只依赖标准库与 httpx 系列。在项目根目录
运行：

```bash
uv run python chapters/01-streaming-agent/src/demo.py
```

完整输出见本章开头的先跑起来一节。注意演示 2 在终端上的实际观感是文字一小段
一小段冒出来的，演示 3 是 Agent 的标准收尾：屏幕上的流式效果照旧，但程序
手里拿到的是一条完整消息。

## 本章小结

- `load_api_key()`：环境变量优先、`.env` 兜底的密钥加载，缺失时响亮失败
- `Message`：`frozen` 数据类，从语言层面保证历史消息不可篡改
- `DeepSeekClient.chat()`：非流式调用，发历史、等全量、解析 `choices[0]`
- `DeepSeekClient.stream()`：SSE 流式调用，异步生成器逐 chunk 产出增量文字
- `DeepSeekClient.stream_message()`：组装分片为完整 `Message`

## 对照官方

官方用 TypeScript 完成了同样的事，两处代码点开对照：

| 官方代码 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/llm/llm-deepseek/src/adapter.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/llm/llm-deepseek/src/adapter.ts) | `DeepSeekClient.stream()` | 官方 DeepSeek 适配器，第 286 行 `accept: text/event-stream` 同样走 SSE |
| [`packages/llm/llm/src/assembler.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/llm/llm/src/assembler.ts) | `stream_message()` | 官方 `BlockAssembler`，第 60-63 行处理 `text-delta` 分片 |

两处差异：官方的组装器同时处理文本、思考、工具调用三种分片，并且
要求每个块以 `block-end` 事件正式收尾，流中途断了会留下明确的失败记录，而
不是静默接受半条消息。工具调用分片留到第 02 章展开，错误处理的分寸会在
第 07 章系统化处理。

## 练习

1. **换一个 system 提示词。** 把 demo 的 `system` 提示词改成用英文回答所有
   问题，重新运行，观察输出变化。同样的请求，为什么只是换了一行 `system`
   文本，模型行为就变了？把这个问题的答案写下来。
2. **读一次真实的错误。** 把 `.env` 里的 Key 故意改错一位，重新运行，观察
   `raise_for_status()` 抛出的异常里状态码是多少，应该为 401。体会响亮失败
   与静默解析的区别：前者一眼定位问题，后者要猜很久。
3. **观察空分片。** 把 `stream()` 里的 `if piece:` 删掉，无条件
   `yield delta.get("content")`，运行后观察输出中多出的 `None`。想想为什么
   某些 delta 没有 `content` 字段，以及删掉检查后下游代码会受到什么影响。
4. **做一次截断实验。** 给请求体加一个 `max_tokens: 20` 字段，协议原生支持，
   运行后观察长回答如何被截断。截断发生时，1.5 节的组装逻辑会收到什么？
   历史里存的还是完整消息吗？
