# 01｜最小流式 Agent：先让模型「边想边说」

> 预计时间：60 分钟 ｜ 前置要求：会运行 Python 文件、知道 API Key 是什么 ｜ 本章调用真实 DeepSeek 模型

你在 ChatGPT 或 DeepSeek 网页版里问过问题。输入框敲下回车后，回答是一个字一个字
「长」出来的——往往第一句话还没看完，后面的内容已经在往外冒。这种体验背后有一个
很基础、也很关键的机制：**流式输出**。

官方 DeepSeek Harness 是一个用 TypeScript 写的 Agent 框架。Agent 与网页聊天最大的
区别在于：Agent 不负责把文字显示给人看，它要把每一轮对话**存进历史**，带着历史
反复调用模型、执行工具，直到完成任务。这个「边生成、边显示、边存档」的分寸，正是
本章要亲手实现的东西。

在本章中，我们将从零实现第一个可运行的小组件：一个能调用 DeepSeek 模型、支持
流式输出、并把回复组装成完整消息的 Python 客户端。这个组件是全书的地基——从第 02
章开始，模型将在这个客户端之上学会调用工具，而「流式展示、完整入史」的规矩从
现在起就不再改变。

## 1.1 在动手之前：认识我们要调用的接口

在开始写代码之前，我们先把「模型服务」这件事本身搞清楚。后面所有章节都在和它
打交道：模型调用是 Agent 的心脏，接口的每一个字段都直接影响后续设计，值得
在第一章投入这十分钟。

### 1.1.1 什么是 API，什么是 OpenAI 兼容接口

模型跑在服务商的服务器上。你的程序要和它通信，需要一个约定好的「对接口」——往
某个网址发一段特定格式的数据，服务器处理完返回一段特定格式的数据。这种对接口
就叫 **API**（Application Programming Interface）。

各家模型厂商（OpenAI、DeepSeek、Moonshot……）的接口格式高度一致，因为大家默认
采用 OpenAI 当年定下的规范，业界称为 **OpenAI 兼容接口**。好处很明显：学会了
一种调用方式，换个厂商只需要改网址和 Key，代码几乎不用动。DeepSeek 完全兼容
这套规范，接口地址是 `https://api.deepseek.com`。

### 1.1.2 一次请求长什么样

调用模型 = 向 `/chat/completions` 发一个 HTTP POST 请求。请求体是一个 JSON，核心
字段有三个：

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

- `model`：用哪个模型。`deepseek-chat` 是 DeepSeek 的通用对话模型。
- `messages`：对话历史，一个消息数组。模型对世界的全部了解都来自这个数组——
  这是最重要的一点：**模型没有记忆，你给什么它看什么**。
- `stream`：`false` 表示「全部想完再给我」，`true` 表示「边想边给我」。

### 1.1.3 三种角色：system / user / assistant

`messages` 里的每条消息都有一个 `role`，三种角色各有分工：

| 角色 | 谁说的话 | 作用 |
|------|----------|------|
| `system` | 系统 | 放在最前面，给模型立规矩（「你是简洁的助手」） |
| `user` | 用户 | 人提出的问题和要求 |
| `assistant` | 模型 | 模型自己的回答 |

`system` 为什么存在？因为模型的默认行为是「热心回答一切」。当你要它扮演特定
角色（代码助手、翻译、客服），把要求写进 `system` 最有效——官方 Harness 的
系统提示词就承担这个职责，第 06 章我们会专门研究它。

### 1.1.4 一次响应长什么样

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

- `choices`：候选回答的数组，一般只有一项。
- `choices[0].message.content`：模型的完整回答文本。这个取值路径贯穿全书，
  第 02 章的工具调用会在这个 `message` 里多出一个 `tool_calls` 字段。

## 1.2 环境准备：Python、uv 与 API Key

### 1.2.1 Python 与 uv

本书代码全部基于 Python 3.11+。在终端运行 `python --version` 确认版本。

包管理方面，我们使用 [uv](https://astral.sh/uv)，一个由 Rust 写的极快 Python 包
管理器。它的核心命令只有一个：

```bash
uv run python 某个文件.py
```

这条命令会先检查项目根目录的 `pyproject.toml`，自动安装缺失的依赖，然后在正确
的虚拟环境里运行你的文件。项目所需的依赖（`httpx`、`httpx-sse` 等）已经写在
`pyproject.toml` 里，无需手动 `pip install`。没有 uv 的话，按官网说明安装一次
即可，之后全程用它。

### 1.2.2 API Key：程序的「身份证」

调用模型服务需要 **API Key**——一个服务商发给你的秘密字符串，用来证明
「这个请求是你发的，费用记你账上」。它本质上是密码：谁拿到谁就能花你的钱。

因此 Key 的处理方式直接影响项目安全：

- **不能硬编码在源码里**：源码要开源、要分享，Key 写进去等于公开。
- **标准做法是放进环境变量**：把秘密存在运行环境里，代码只在运行时读取。

本地学习时，最方便的形式是 `.env` 文件：一个放在项目根目录的纯文本文件，每行
一个 `名字=值`。在项目根目录创建 `.env`（根目录已有 `.env.example` 模板），写入：

```bash
DEEPSEEK_API_KEY=sk-你的key
```

打开 `.gitignore` 会发现 `.env` 已经在忽略列表里——git 永远不会跟踪它，上传
GitHub 也不会泄露。

### 1.2.3 实现 load_api_key

现在写本章第一段代码。`load_api_key()` 按「环境变量优先，`.env` 兜底」的顺序
读取 Key：

```python
import os
from pathlib import Path


def load_api_key() -> str:
    # 第一步：环境变量（部署到服务器时的标准做法）
    from_env = os.getenv("DEEPSEEK_API_KEY")
    if from_env:
        return from_env

    # 第二步：项目根目录的 .env 文件（本地学习更方便）
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

- `os.getenv("DEEPSEEK_API_KEY")`：读环境变量，没有就返回 `None`。
- `Path(__file__).resolve().parents[3]`：`__file__` 是这段代码自己的文件路径。
  文件位于 `chapters/01-streaming-agent/src/`，`.`parents[0]` 是 `src/`，
  `.parents[1]` 是 `01-streaming-agent/`，`.parents[2]` 是 `chapters/`，
  `.parents[3]` 才是项目根目录。用 `resolve()` 把相对路径变成绝对路径，
  这样无论从哪个目录启动程序，都能定位到 `.env`。
- 逐行扫描 `.env`，找到 `DEEPSEEK_API_KEY=` 开头的行，用 `split("=", 1)`
  取等号后面的值（`1` 表示只切第一刀，Key 里即使有等号也不会被切坏）。
- `strip().strip('"').strip("'")`：去掉首尾空格和可能的引号。
- 最后 `raise RuntimeError(...)`：Key 缺失时立刻失败并说清原因。这里体现
  一个贯穿全书的习惯——**坏状态要在最靠近它的地方响亮地失败**，而不是
  带着一个空 Key 继续跑，直到发请求时才报一个莫名其妙的 401。

## 1.3 定义消息：Message 与不可变性

在写客户端之前，我们先定义贯穿全书的数据结构：**一条对话消息**。

### 1.3.1 为什么先定义消息

回头看 1.1.2 节：模型对世界的全部了解来自 `messages` 数组。Agent 的每一轮运行
都在做同一件事——把历史消息发给模型，把模型的新回复追加进历史。对话历史是
Agent 的**核心数据**，它的数据结构必须先想清楚。

Python 的 `dataclass` 是定义这类「数据类」的标准工具：它帮我们省掉手写
`__init__` 的样板代码。我们对 `Message` 加一个关键修饰：`frozen=True`。

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    role: str     # "system"（规矩）/"user"（人）/"assistant"（模型）
    content: str  # 消息正文
```

### 1.3.2 frozen 的底层原理

`frozen=True` 做了什么？`dataclass` 默认生成的 `__init__` 用 `self.role = role`
这样的方式给属性赋值；而 `frozen=True` 会额外生成一个 `__setattr__` 方法，
**拒绝任何后续的属性写入**。于是：

```python
m = Message(role="user", content="你好")
m.content = "篡改"   # 抛 FrozenInstanceError
```

为什么 Agent 的消息必须不可变？对话历史会被反复读取——每一轮都要完整地发给
模型，压缩、持久化、界面展示都要读它。任何一处代码悄悄改动了历史内容，后续
所有行为都会跟着错，而且极难排查。把「禁止修改」变成语言层面的约束，这类
bug 从根源上被消灭。官方 Harness 的消息模型同样深冻结（deep-freeze）每条
消息，第 05 章的事件日志还会再次用到这个思想。

## 1.4 第一次调用：chat() 一次拿回完整回答

接下来，我们来实现第一种调用方式，也是最朴素的形态：把整段历史发过去，等模型
全部想完，一次性拿回完整回答。先写出客户端类的骨架和 `chat()` 方法：

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

逐段理解这段代码：

- **`httpx`**：Python 生态主流的 HTTP 客户端库，接口风格与经典库 `requests`
  一致，同时支持同步和异步两种用法（本章两节会各用一次）。`with httpx.Client(...)`
  保证请求结束后连接被正确关闭。
- **`Authorization` 头**：`Bearer <key>` 是 OpenAI 兼容接口的统一认证格式，
  DeepSeek 服务器读到这个头就知道你是谁、有没有额度。
- **`Content-Type` 头**：告诉服务器「我发来的是 JSON 格式的请求体」。
- **`json={...}`**：httpx 的便捷参数，自动把字典序列化成 JSON 并设置好格式头。
  请求体的三个字段正是 1.1.2 节讲的 `model` / `messages` / `stream`。
- **`"stream": False`**：本节的开关——「等我全部想完，一次性给我」。
- **`raise_for_status()`**：HTTP 状态码非 2xx 时抛出带状态码的异常。401 表示
  Key 错误，429 表示请求过频，502 表示服务端问题。**不检查状态码是新手最常见
  的坑**——请求失败时直接往下解析，会得到一个莫名其妙的 KeyError。
- **返回路径** `data["choices"][0]["message"]["content"]`：对应 1.1.4 节的响应
  结构，逐层取到回答文本。

把 `chat()` 用在 demo 里，运行得到（本章 demo 文件是
`chapters/01-streaming-agent/src/demo.py`，完整演示在 1.7 节）：

```
完整回答：流式输出是边生成边传输数据，无需等待全部完成后才显示，像打字机一样逐字呈现结果。
```

到这里，我们已经能和模型完成一次完整的「提问—回答」。但请注意这个细节：
上面这行字是**等模型全部生成完**才出现的。如果回答很长，用户会盯着空白屏幕
等上几十秒。下一节解决这个问题。

## 1.5 流式调用：stream() 边生成边显示

`chat()` 已经能完成一次完整的「提问—回答」，但它有一个明显的体验短板。
我们先看清问题，再研究解决它的协议，最后动手实现。

### 1.5.1 问题：几十秒的空白等待

模型生成文字是逐字「算」出来的，一段几百字的回答可能要花 20 秒以上。非流式
模式下，这 20 秒里用户什么都看不到。网页版聊天没有这个问题——答案一个字一个
字往外蹦，因为网页版用的是**流式接口**。

流式接口的约定很简单：请求里把 `stream` 设为 `true`，服务器就不再一次性返回
完整回答，而是**生成一小段就立刻推送一小段**，直到全部完成。这些连续推送的
小片段称为 **chunk**（分片）。模型说 500 个字，客户端可能收到几十个 chunk，
每个 chunk 只包含几个字。

### 1.5.2 流式的传输协议：SSE

服务器「持续推送」数据，用的是什么协议？答案是 **SSE**（Server-Sent
Events，服务器推送事件）。

普通的 HTTP 响应是「一问一答」：服务器把完整响应体发完就关闭连接。SSE 的
响应不同：服务器发完响应头后**保持连接不关**，之后持续不断地写入一条条
格式如下的数据：

```
data: {"choices":[{"delta":{"content":"流式"}}]}

data: {"choices":[{"delta":{"content":"输出"}}]}

data: [DONE]
```

三条规律：

1. 每条推送以 `data: ` 开头，后面跟一段文本，空行分隔。
2. 内容是**增量**（delta）而不是全量：每块只带新生成的一小段文字，所以
   取值的路径是 `choices[0].delta.content`，与 1.4 节的 `message.content`
   只差一个字段名。
3. 结束信号是固定的一行：`data: [DONE]`。收到它表示模型说完了，可以断开。

`httpx-sse` 库帮我们完成协议解析——连接管理、按空行切分事件、剥离 `data: `
前缀，我们拿到的就是干净的 `event.data` 字符串。

### 1.5.3 两个必要的 Python 概念：async 与 yield

流式连接会「挂着等数据」，用同步代码处理它会卡死整个程序，因此本节引入两个
新的 Python 概念。这里先建立直觉，后面章节会反复用到：

**async / await（异步）**。同步程序的执行流是一条直线：调用函数 → 等它返回 →
继续。网络等待期间，程序整个停住。异步程序把「等待」和「执行」分开：遇到
`await` 时先挂起当前任务，事件循环去处理其他任务，数据到了再回来继续。这样
一条连接在等数据时，程序还能干别的事。本书不要求你掌握事件循环的实现细节，
只要记住写法：异步函数用 `async def` 定义，调用时用 `await`，运行入口用
`asyncio.run(...)`。

**yield（生成器）**。函数里出现 `yield`，它就不再是普通函数，而是**生成器**：
执行到 `yield` 时把值「吐」给调用方，然后暂停在原地；调用方下一次迭代时，
从暂停处继续。于是调用方可以拿到一个值立刻处理（打印到屏幕），而生成器继续
等下一个 chunk——这正是流式消费的天然形态。

### 1.5.4 实现 stream()

```python
import json

import httpx
from httpx_sse import aconnect_sse


class DeepSeekClient:
    # ...（上一节的字段和 chat() 略）

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

- **`async with httpx.AsyncClient(...)`**：异步版 HTTP 客户端。`async with` 与
  普通 `with` 作用相同（用完自动关闭），区别是进出块时可以 `await` 等待。
- **`aconnect_sse(...)`**：httpx-sse 提供的异步 SSE 连接入口。第一个参数是
  http 客户端，然后是请求方法和地址，后面与 `chat()` 一样传 `headers` 和
  `json`。注意 `"stream": True`。
- **`async for event in event_source.aiter_sse()`**：每收到一条 SSE 事件迭代
  一次，`event.data` 就是去掉 `data: ` 前缀后的文本。
- **`if event.data == "[DONE]": break`**：DeepSeek 的结束标记，收到就退出循环。
- **`payload["choices"][0].get("delta", {})`**：`get` 而不是下标——个别事件里
  可能没有 `delta` 字段（例如只带用量统计的事件），用 `get` 安全地给空字典。
- **`if piece: yield piece`**：只产出非空文本。有些 delta 的 `content` 是
  `None`（比如流刚开始时的元信息），跳过它们。

调用方用 `async for` 消费生成器，配合 `flush=True` 让文字立刻上屏：

```python
async for piece in client.stream(HISTORY):
    print(piece, end="", flush=True)
```

`flush=True` 不可省：Python 的 `print` 默认带缓冲，不刷新的话文字会攒在内存
里，终端上看不到「逐字蹦出」的效果。

## 1.6 组装：stream_message() 让历史只存完整消息

流式解决了「看得快」，同时制造了一个新问题。设想两种处理分片的方式，比较
一下后果：

- **方式一：每收到一个 chunk 就往历史里塞一条消息。** 下一轮请求将带着几十条
  碎消息发给模型，token 浪费还在其次，模型看到的历史会异常混乱。
- **方式二：流在中途断掉（网络抖动、用户取消）。** 如果历史里已经塞了半句话，
  这条「半个回答」会永远留在历史里，后续每一轮都带着它。

官方 Harness 对这个问题给出的答案是 **BlockAssembler（块组装器）** 组件：流式
分片一边实时展示，一边被组装器暂存；流正常结束后，组装器产出一条完整消息，
这条消息才被写进历史。值得注意的是，官方把这个「完整才入史」的边界守得比我们
更严——连每个块的结束都有专门的 `block-end` 事件标记。我们先实现它的简化版：

```python
class DeepSeekClient:
    # ...（前两节略）

    async def stream_message(self, messages: list[Message]) -> Message:
        pieces: list[str] = []
        async for piece in self.stream(messages):
            pieces.append(piece)
        return Message(role="assistant", content="".join(pieces))
```

- `pieces` 列表暂存所有分片；`"".join(pieces)` 把它们按顺序拼成完整文本。
- 返回的是 `Message` 实例——`frozen=True` 保证这条消息从此不可篡改。
- 流中途抛异常时，函数直接以异常结束，**半成品消息根本不会产生**——这就是
  「历史只存完整消息」的落地方式。

三行核心逻辑，但它是全书最重要的习惯之一。从第 02 章起，模型开始调用工具，
历史里会出现工具调用、工具结果等更多种类的消息，而「流式展示、完整入史」
的规矩自始至终不变。

## 1.7 跑一遍完整 demo

到这里，本章的全部代码已经写完。接下来，我们把三种调用方式连起来跑一遍，
看看整体效果。本章代码共两个文件，全部自包含（只依赖标准库与 httpx 系列）：

```
chapters/01-streaming-agent/src/
├── client.py   # 本章实现：load_api_key / Message / DeepSeekClient
└── demo.py     # 依次演示 chat、stream、stream_message 三种方式
```

在项目根目录运行：

```bash
uv run python chapters/01-streaming-agent/src/demo.py
```

完整输出（回答内容由真实模型生成，每次略有不同）：

```
============================================================
演示 1：非流式调用（一次拿回完整回答）
============================================================
完整回答：流式输出是边生成边传输数据，无需等待全部完成后才显示，像打字机一样逐字呈现结果。

============================================================
演示 2：流式调用（边生成边显示）
============================================================
逐分片输出：流式输出是指数据在生成过程中逐段（如逐词或逐块）实时传输给用户，而非等待全部完成后一次性返回。

============================================================
演示 3：流式 + 组装成完整消息（Agent 的标准做法）
============================================================
进入历史的消息：role='assistant', 长度=45 字
消息内容：流式输出是指数据或内容在生成过程中分块、连续地传送给接收方，而非等待全部完成后一次性输出。
```

注意演示 2 在终端上的实际观感：文字是**一小段一小段**冒出来的。演示 3 是
Agent 的标准收尾：屏幕上的流式效果照旧，但程序手里拿到的是一条完整消息。

想亲眼验证 `frozen` 的约束，打开 `demo.py`，把最后一段被注释掉的
`message.content = "篡改"` 恢复，再跑一次——程序会抛出
`FrozenInstanceError`，证明历史消息确实无法修改。

## 1.8 本章小结：亲手写了什么

- `load_api_key()`：环境变量优先、`.env` 兜底的密钥加载，缺失时响亮失败
- `Message`：`frozen` 数据类，从语言层面保证历史消息不可篡改
- `DeepSeekClient.chat()`：非流式调用——发历史、等全量、解析 `choices[0]`
- `DeepSeekClient.stream()`：SSE 流式调用——异步生成器逐 chunk 产出增量文字
- `DeepSeekClient.stream_message()`：组装分片为完整 `Message`，守住「完整入史」

## 1.9 对照官方 DSH

官方用 TypeScript 完成了同样的事，两处代码值得点开对照：

| 官方代码 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/llm/llm-deepseek/src/adapter.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/llm/llm-deepseek/src/adapter.ts) | `DeepSeekClient.stream()` | 官方 DeepSeek 适配器，第 286 行 `accept: text/event-stream` 同样走 SSE |
| [`packages/llm/llm/src/assembler.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/llm/llm/src/assembler.ts) | `stream_message()` | 官方 `BlockAssembler`：第 60-63 行处理 `text-delta` 分片 |

两处差异值得知道：官方的组装器同时处理**文本、思考（reasoning）、工具调用**
三种分片，并且要求每个块以 `block-end` 事件正式收尾——流中途断了会留下明确
的失败记录，而不是静默接受半条消息。工具调用分片留到第 02 章展开；错误处理
的分寸会在第 07 章（Agent 循环）系统化处理。

## 1.10 练习

接下来，我们通过四道练习巩固本章内容。每道练习都要求你操作、观察、并解释
现象背后的原因——只跑通不够，讲得出为什么才算学会。

1. **改人设**：把 demo 的 `system` 提示词改成「用英文回答所有问题」，观察输出
   变化。思考：同样的请求，为什么只是换了一行 `system` 文本，模型行为就变了？
2. **读错误**：把 `.env` 里的 Key 故意改错一位，重新运行，观察
   `raise_for_status()` 抛出的异常里状态码是多少（应为 401），体会「响亮失败」
   与「静默解析」的区别。
3. **观察空分片**：把 `stream()` 里的 `if piece:` 删掉，无条件 `yield
   delta.get("content")`，运行后观察输出中多出的 `None`，理解为什么某些
   delta 没有 `content` 字段。
4. **截断实验**：给请求体加一个 `max_tokens: 20` 字段（协议原生支持），运行后
   观察长回答如何被截断，以及截断对 1.6 节「完整入史」意味着什么。
