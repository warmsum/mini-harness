# 06｜请求信封：系统提示词与工具清单的组装

> 预计时间：55 分钟 ｜ 前置：完成第 05 章 ｜ 本章调用真实 DeepSeek 模型

第 05 章结束时，每次请求发给模型的东西长这样：

```
messages = [system 提示词, ...会话投影出来的消息]
tools = [calculator]   # 一张写死的列表
```

系统提示词是一整块手写的字符串，工具清单是一张裸列表。对只有一个计算器
的玩具 Agent，这样没问题；但真实 Agent 的提示词会来自四面八方——人设、
安全规则、工具说明各是一段，而工具清单会随注册的动态变化。本章把这两样
东西变成**可组装**的：多个贡献者各贡献一段，组装器统一排序拼接。

官方把这两样东西统称为请求的**信封**（envelope）——与消息历史并列、
每次请求都要携带的部分。官方的 `core/system-prompt` 文档开头写道：
「系统提示词组装注册表。插件可以贡献有序段、工具 schema 和具名变量。
循环在每个步骤组装一次，并将结果渲染为完整的模型提示词」。

## 6.1 原理：系统提示词为什么要「组装」

先看一个真实 Agent 的提示词都由哪些部分组成：

| 段 | 内容举例 | 谁贡献的 |
|----|----------|----------|
| 人设 | 「你是一个数学助手……」 | 人设插件 |
| 工具目录 | 「可用工具：calculator……」 | 工具注册表 |
| 安全规则 | 「所有计算必须经工具，禁止心算」 | 沙箱插件 |
| 运行时信息 | 「当前模型：deepseek-chat」 | 框架自身 |

四个来源，四个插件，各自只关心自己那一段。如果不组装，谁写整个提示词？
只有两种结局：要么全部塞进主流程（又是第 03 章的上帝函数），要么让
插件互相改同一个字符串（顺序即灾难）。

组装器（assembler）的设计把「贡献」和「拼接」分开：

1. 每个插件调用 `section(name, text, order)` 贡献一段；
2. 组装时按 `order` 排序拼接，同 order 按名字排序——**组装结果只由
   order 决定，与插件加载顺序无关**（确定性，官方 :14 对 toolOrder
   也是同样要求）。

官方还支持「具名变量」：段文本里写 `{{model}}` 这样的占位符，组装时
用运行时值替换——提示词需要「当前模型名」这类只有运行时才知道的信息
时，变量机制避免了字符串拼接的脆弱。

## 6.2 PromptAssembler：段的贡献与拼接

```python
@dataclass(frozen=True)
class PromptSection:
    order: int
    name: str
    text: str


class PromptAssembler:
    def __init__(self) -> None:
        self._sections: list[PromptSection] = []

    def section(self, name: str, text: str, order: int = 0) -> None:
        """贡献一段。同名段后到者覆盖先到者。"""
        self._sections = [s for s in self._sections if s.name != name]
        self._sections.append(PromptSection(order=order, name=name, text=text))

    def render(self, variables: dict[str, str] | None = None) -> str:
        ordered = sorted(self._sections, key=lambda s: (s.order, s.name))
        text = "\n\n".join(section.text for section in ordered)
        for name, value in (variables or {}).items():
            text = text.replace("{{" + name + "}}", value)
        return text
```

三个要点：

- **排序键 `(order, name)`**：order 是主键（数字小的在前），name 是
  兜底——两个段 order 相同时按名字排，保证结果稳定。官方用负数 order
  放最前面的固定开场白（`includeHarnessIdentity` 默认顺序 −100），
  我们的人设段用 0，规则段用 100，同一条思路。
- **同名覆盖**：同一个名字的段重复贡献时后者胜。真实场景里这是
  「agent 级人设覆盖全局人设」的基础（官方 :13 的 persona 遮蔽机制）。
- **变量替换**：`render(variables)` 把 `{{name}}` 换成真实值。注意
  替换只发生在 render 时——段文本本身保持模板原样，同一份模板可以
  用不同的变量渲染出不同结果。

## 6.3 ToolRegistry：工具从列表到注册表

第 02 章的工具清单 `list[Tool]` 有一个隐患：`Tool` 里同时装着「给模型
看的说明书」（name/description/parameters）和「给程序跑的执行器」
（execute）。把整个对象传给模型侧逻辑，等于把执行器也暴露了出去。
注册表把这两个接口分开：

```python
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f'工具 "{tool.name}" 已被注册')
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        del self._tools[name]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        """投影出「给模型看的说明书清单」：不含 execute。"""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self._tools.values()
        ]
```

`schemas()` 是注册表与裸列表的关键区别：模型侧逻辑拿到的永远只是
说明书投影，执行器留在程序侧。这个分离在官方叫 **schema 投影**——
`core/tools` 文档里 `ctx.tools.schemas(scope)` 返回「该作用域可见的
所有 schema（不含 execute 函数）」（官方 :24）。

## 6.4 接进循环：信封的两半

第 05 章的 `run_agent` 只改两处：

```python
def run_agent(client, registry, assembler, user_prompt,
              max_turns=10, variables=None) -> Session:
    tools = registry.all()
    tools_by_name = {tool.name: tool for tool in tools}
    session = Session()
    # ...turn/start、user/message 与第 05 章相同

    for turn in range(max_turns):
        messages = [
            Message(role="system", content=assembler.render(variables)),
            *session.derive_messages(),
        ]
        reply = client.chat(messages, tools)
        # ...其余与第 05 章相同
```

请求信封的两半各就各位：system 由组装器产出，tools 由注册表产出。
循环本身不再知道提示词有几段、工具是谁注册的——这正是第 03 章
「组织」痛点的解药：主流程只剩骨架，细节都在贡献者手里。

## 6.5 跑一遍完整 demo

```bash
uv run python chapters/06-prompt-tools/src/demo.py
```

真实输出：

```
=== ① 组装出的系统提示词 ===
你是 小算，一个数学助手。遇到算式时先调用 calculator 工具计算，再基于计算结果回答。

回答要简洁：先给结论，再给过程。

=== ② 注册表投影出的工具说明书（模型看到的清单） ===
[
  {
    "name": "calculator",
    "description": "计算一个四则运算表达式，支持 + - * / 与括号，例如 '1+2*3'",
    "parameters": {
      "type": "object",
      "properties": {
        "expression": {
          "type": "string",
          "description": "要计算的数学表达式，例如 '1+2*(3-1)'"
        }
      },
      "required": ["expression"]
    }
  }
]
  ← 注意：只有 name/description/parameters，没有 execute

=== ③ 真实跑一遍 ===
  [assistant] 结论：1 + 2 × 3 = 7
  过程：根据运算优先级，先算乘法 2 × 3 = 6，再算加法 1 + 6 = 7。
```

三节对应三个机制：① 人设段（order 0）+ 规则段（order 100）按序拼接，
`{{name}}` 被替换成「小算」；② 注册表投影出的说明书恰好是模型需要
的形状，`execute` 不见踪影；③ 组装出的信封驱动了一次真实对话。

## 6.6 本章小结：亲手写了什么

- `PromptSection` / `PromptAssembler`：段的贡献、确定性排序（order+name）、
  同名覆盖、变量替换
- `ToolRegistry`：注册查重、注销、`schemas()` 说明书投影
- 循环改造：请求信封的两半（组装 system + 注册表 tools）

## 6.7 对照官方 DSH

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/core/system-prompt/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/system-prompt/README.zh.md) | `PromptAssembler` | 官方组装注册表（第 5 行）；`section()` 贡献段（第 20 行）；`variable()` 具名变量（第 24 行） |
| 同上（第 13-14 行） | 排序与覆盖 | 官方 persona 是顺序 0 的段、可被 agent 作用域遮蔽；工具顺序有专门的 toolOrder 配置 |
| [`packages/core/tools/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/tools/README.zh.md) | `ToolRegistry` | 官方注册表（第 20 行 register）与 schema 投影（第 24 行） |

官方比我们多一块：工具 schema 本身也作为**提示词段**注入（工具目录段），
由组装器统一渲染——模型看到的工具说明与 system 提示词是一体的。我们
把两者分开渲染（协议原生 tools 字段），效果等价、结构更直观，差异
在练习 3 里展开。

## 6.8 练习

1. **顺序实验**：把规则段的 order 改成 −10，观察组装结果中规则段是否
   排到人设段前面；再解释为什么「确定性」对调试至关重要。
2. **同名覆盖**：贡献两个同名段（不同内容、不同 order），观察哪个
   生效；把这个行为与官方「agent 人设遮蔽全局人设」联系起来。
3. **工具目录段**：仿照官方，把 `registry.schemas()` 渲染成一段文本
   （如「可用工具：calculator——计算四则运算……」）作为 order 50 的段
   贡献给组装器；对比「协议 tools 字段」与「写进提示词」两种方式的
   差异（提示：模型是否还会按 JSON Schema 传参）。
4. **变量缺省**：render 时不传 variables（demo 第 ③ 节之前改掉传参），
   观察 `{{name}}` 原样进入请求后模型如何理解它。
