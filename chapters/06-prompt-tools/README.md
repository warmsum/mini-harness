# 06｜请求 envelope 组装

> 预计时间：55 分钟 ｜ 前置：完成第 05 章 ｜ 本章调用真实 DeepSeek 模型

第 05 章结束时，每次发给模型的请求长这样：

```python
messages = [Message(role="system", content=system_prompt), *session.derive_messages()]
tools = [calculator]  # 一张写死的列表
```

系统提示词是一整块手写的字符串，工具清单是一张裸列表。对只有一个计算器
的玩具 Agent，这样没问题；真实 Agent 的提示词来自四面八方——人设、安全
规则、工具说明各是一段，工具清单随注册动态变化，还夹着模型名这类只有
运行时才知道的值。本章把这两样东西变成可组装的：多个贡献者各贡献一段，
组装器统一排序拼接，注册表统一管理工具并投影出给模型的说明书。

官方把这两样与消息历史并列、每次请求都要携带的部分统称请求的 envelope。
官方 core/system-prompt 文档开篇写明：系统提示词组装注册表，插件可以贡献
有序段、工具 schema 和具名变量，循环在每个步骤组装一次，并将结果渲染为
完整的模型提示词。

## 6.1 系统提示词为什么要组装

先看一个真实 Agent 的提示词由哪些部分组成：

| 段 | 内容举例 | 贡献者 |
|----|----------|--------|
| 人设 | 你是一个数学助手，遇到算式先调用工具 | 人设插件 |
| 规则 | 回答先给结论，再给过程 | 沙箱插件 |
| 工具目录 | 可用工具：calculator…… | 工具注册表 |
| 运行时信息 | 当前模型：deepseek-chat | 框架自身 |

四个来源各自只关心自己那一段。不组装只有两种结局：全部塞进主流程，回到
第 03 章说过的上帝函数；或者让插件互相改写同一个字符串，顺序即灾难。

组装器把贡献和拼接分开：每个插件调用 `section(name, text, order)` 贡献
一段，组装时按 order 排序拼接。组装结果只由 order 决定，与插件加载顺序
无关，这是确定性的要求——官方对工具顺序有同样的要求，toolOrder 配置
写明注册顺序只是插件加载时序的产物，不能影响最终结果。

官方还支持具名变量：段文本里写 `{{model}}` 这样的占位符，组装时用运行时
值替换。提示词需要模型名、当前目录这类只有运行时才知道的信息时，变量
机制替代了脆弱的字符串拼接。

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

三个机制：

1. 排序键 `(order, name)`。order 是主键，数字小的在前；name 是兜底，两个
   段 order 相同时按名字排，结果稳定。官方用负数 order 放固定开场白，
   includeHarnessIdentity 的默认顺序是 −100，0 是 persona 的位置，工具
   引导用 100–199。教学版的人设段用 0、规则段用 100，同一条思路。
2. 同名覆盖。同名段重复贡献时后者胜。真实场景里这是 agent 级人设遮蔽
   全局人设的基础，官方 persona 正是顺序 0 的段，被 agent 作用域的贡献
   遮蔽。
3. 变量替换。`render(variables)` 把 `{{name}}` 换成真实值。替换只发生在
   render 时，段文本保持模板原样，同一份模板可以配不同的变量渲染出不同
   结果。

## 6.3 ToolRegistry：从列表到注册表

第 02 章的工具是 `list[Tool]`，`Tool` 里同时装着两样东西：给模型看的
说明书，name、description、parameters；给程序跑的执行器，execute。把整个
对象传给模型侧逻辑，执行器也一并暴露。注册表把两个接口分开：

```python
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f'工具 "{tool.name}" 已被注册')
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        """投影出给模型看的说明书清单：不含 execute。"""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self._tools.values()
        ]
```

注册是入口校验点：重名即抛错，两个同名工具会让模型传参产生歧义，必须在
入口挡掉。`schemas()` 是注册表与裸列表的关键区别：模型侧逻辑拿到的永远
只是说明书投影，执行器留在程序侧。官方 core/tools 文档写明，schemas 返回
该作用域可见的所有 schema，不含 execute 函数。这个分离在官方叫 schema
投影，也是第 12 章技能和所有后续工具章节的基础。

## 6.4 接进循环：envelope 的两半

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

请求 envelope 的两半各就各位：system 由组装器产出，tools 由注册表产出。
循环本身不再知道提示词有几段、工具是谁注册的。第 03 章组织痛点的解药
在这里兑现：主流程只剩骨架，细节都在贡献者手里。

## 6.5 跑一遍完整 demo

```bash
uv run python chapters/06-prompt-tools/src/demo.py
```

真实输出，模型回复内容每次不同，组装结果稳定：

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
  ← 只有 name/description/parameters，没有 execute

=== ③ 真实跑一遍 ===
  [assistant]
  [assistant] 1+2*3 = **7**

先算乘法 2×3=6，再加 1，得 7。
```

三节对应三个机制：① 人设段（order 0）与规则段（order 100）按序拼接，
`{{name}}` 被替换成小算；② 注册表投影出的说明书恰好是模型需要的形状，
execute 不见踪影；③ 组装出的 envelope 驱动了一次真实对话，第一行空
assistant 消息是模型请求工具的那一步，内容为空、只带 tool_calls，投影
后正文留空。

## 本章小结

- `PromptSection` / `PromptAssembler`：段的贡献、确定性排序（order+name）、
  同名覆盖、变量替换
- `ToolRegistry`：注册查重、`schemas()` 说明书投影，模型接口与执行接口分离
- 循环改造：请求 envelope 的两半，组装出的 system 与注册表的 tools

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/core/system-prompt/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/system-prompt/README.zh.md) | `PromptAssembler` | 组装注册表定义在第 5 行；`section()` 贡献段在第 20 行；`variable()` 具名变量在第 24 行 |
| 同上，第 13 行 | 同名覆盖 | 官方 persona 是顺序 0 的段，agent 作用域的贡献将其遮蔽，与教学版同名覆盖同构 |
| 同上，第 11、14 行 | 排序确定性 | 官方固定开场白顺序 −100；toolOrder 显式指定工具顺序，未列工具按名称字典序，注册顺序不影响结果 |
| [`packages/core/tools/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/core/tools/README.zh.md) | `ToolRegistry` | `register` 在第 20 行，`schemas()` 不含 execute 在第 24 行 |

官方比教学版多一块：工具 schema 属于组装结果本身。core/tools 文档写明
注册表通过 `ctx.systemPrompt.tools()` 自动把工具 schema 送入系统提示词
组装，模型获知自己能做什么是一个连贯整体，适配器再把 schema 作为独立
wire 字段传输。教学版把两者分开渲染，协议原生的 tools 字段直接交给
client，结构更直观，差异在练习 3 展开。

## 练习

1. **顺序实验。** 把规则段的 order 改成 −10，跑 demo 观察规则段是否排到
   人设段前面；再解释确定性排序对调试的价值：同一个 bug 为什么必须能
   稳定复现。
2. **同名覆盖。** 贡献两个同名段，内容与 order 都不同，观察哪个生效；
   把这个行为与官方 agent 人设遮蔽全局人设联系起来，说明两者各自解决
   什么场景。
3. **工具目录段。** 仿照官方，把 `registry.schemas()` 渲染成一段文本，作为
   order 50 的段贡献给组装器。对比协议 tools 字段与写进提示词两种方式，
   模型是否还会按 JSON Schema 传参，token 消耗有什么差异。
4. **变量缺省。** 在 demo 里删掉 `variables={"name": "小算"}` 这个传参，
   观察 `{{name}}` 原样进入请求后模型如何理解它。官方对未提供值的变量
   会直接抛错，教学版选择原样通过，权衡一下两种策略。
