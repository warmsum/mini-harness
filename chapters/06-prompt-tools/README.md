# 06｜请求 envelope 组装

> 预计时间：55 分钟 ｜ 前置：完成第 05 章 ｜ 本章调用真实 DeepSeek 模型

第 05 章结束时，每次发给模型的请求长这样：

```python
messages = [Message(role="system", content=system_prompt), *session.derive_messages()]
tools = [calculator]  # 一张写死的列表
```

系统提示词目前是一段手写字符串，工具清单则是固定列表。当 Agent 继续加入人设、安全规则、工具说明和运行时信息后，这些内容会来自不同模块，工具也会动态注册。本章把二者改造成可组合结构：不同模块分别贡献提示词片段，组装器负责稳定排序；工具注册表负责管理工具，并生成模型需要的说明书。

官方把这两样与消息历史并列、每次请求都要携带的部分统称请求的 envelope。官方 core/system-prompt 文档开篇写明：系统提示词组装注册表，插件可以贡献有序段、工具 schema 和具名变量，循环在每个步骤组装一次，并将结果渲染为完整的模型提示词。

## 学习目标

完成本章后，你将能够：

- 把系统提示词拆成可独立贡献和排序的多个段；
- 在渲染提示词时替换运行时变量，并保证结果稳定；
- 使用 `ToolRegistry` 管理工具并拒绝重名注册；
- 区分模型看到的工具 schema 与程序执行的工具函数。

## 6.1 系统提示词为什么要组装

先看一个真实 Agent 的提示词由哪些部分组成：

| 段 | 内容举例 | 贡献者 |
|----|----------|--------|
| 人设 | 你是一个数学助手，遇到算式先调用工具 | 人设插件 |
| 规则 | 回答先给结论，再给过程 | 沙箱插件 |
| 工具目录 | 可用工具：calculator…… | 工具注册表 |
| 运行时信息 | 当前模型：deepseek-chat | 框架自身 |

四个来源各自只负责自己的内容。如果全部由主流程拼接，主流程会再次承担过多职责；如果让插件共同改写一个字符串，最终顺序又会依赖加载时机，难以复现和调试。

组装器把贡献和拼接分开：每个插件调用 `section(name, text, order)` 贡献一段，组装时按 order 排序拼接。组装结果只由 order 决定，与插件加载顺序无关，这是确定性的要求——官方对工具顺序有同样的要求，toolOrder 配置写明注册顺序只是插件加载时序的产物，不能影响最终结果。

官方还支持具名变量：段文本里写 `{{model}}` 这样的占位符，组装时用运行时值替换。提示词需要模型名、当前目录这类只有运行时才知道的信息时，变量机制替代了脆弱的字符串拼接。

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
        self._variables: dict[str, Callable[[], str]] = {}

    def section(self, name: str, text: str, order: int = 0) -> None:
        if any(section.name == name for section in self._sections):
            raise ValueError(f'提示词段 "{name}" 已被注册')
        self._sections.append(PromptSection(order=order, name=name, text=text))

    def variable(self, name: str, provider: Callable[[], str]) -> None:
        self._variables[name] = provider

    def render(self, variables: dict[str, str] | None = None) -> str:
        ordered = sorted(self._sections, key=lambda s: s.order)
        text = "\n\n".join(section.text for section in ordered)
        resolved = {name: provider() for name, provider in self._variables.items()}
        resolved.update(variables or {})
        for name, value in resolved.items():
            text = text.replace("{{" + name + "}}", value)
        unresolved = re.findall(r"{{([a-zA-Z_][a-zA-Z0-9_]*)}}", text)
        if unresolved:
            raise KeyError(f"未注册的提示词变量: {', '.join(unresolved)}")
        return text
```

三个机制：

1. 只按 `order` 排序；Python 的稳定排序会让相同 order 保持注册顺序。官方也保留同序贡献的注册顺序，而不是另按名称重排。
2. 同一层的同名段立即报错。官方的“遮蔽”发生在不同 scope 层之间，不能把它误解为同一注册表里后者静默覆盖前者。
3. 变量由 `variable(name, provider)` 注册，provider 每次 render 重新求值。模板引用了未知变量会直接失败，避免把 `{{typo}}` 发给模型。

## 6.3 ToolRegistry：从列表到注册表

第 02 章的工具是 `list[Tool]`，`Tool` 里同时装着两样东西：给模型看的说明书，name、description、parameters；给程序跑的执行器，execute。把整个对象传给模型侧逻辑，执行器也一并暴露。注册表把两个接口分开：

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
        return [self._tools[name] for name in sorted(self._tools)]

    def schemas(self) -> list[dict[str, Any]]:
        """投影出给模型看的说明书清单：不含 execute。"""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self.all()
        ]
```

注册是入口校验点：重名即抛错。`all()` 默认按工具名排序，使相同工具集合产生稳定的请求 envelope；官方也默认按名称排序，只有显式 `toolOrder` 才改变顺序。`schemas()` 只投影模型需要的说明书，不包含执行器。

## 6.4 接进循环：envelope 的两半

第 05 章的 `run_agent` 只改两处：

```python
def run_agent(client, registry, assembler, user_prompt,
              max_steps=10, variables=None) -> Session:
    tools = registry.all()
    tools_by_name = {tool.name: tool for tool in tools}
    session = Session()
    # ...turn/start、user/message 与第 05 章相同

    for step in range(1, max_steps + 1):
        messages = [
            Message(role="system", content=assembler.render(variables)),
            *session.derive_messages(),
        ]
        reply = client.chat(messages, tools)
        # ...其余与第 05 章相同
```

至此，请求 envelope 中的 system 由组装器生成，tools 由注册表提供。循环不再需要知道提示词由几段组成，也不关心工具由哪个模块注册。第 03 章提出的职责拆分在这里落到请求组装流程中。

## 6.5 运行完整示例

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

三部分输出分别验证了三个机制：① 人设段（order 0）与规则段（order 100）按序拼接，`{{name}}` 被替换成小算；② 注册表只输出模型需要的说明字段，不包含 execute；③ 组装后的 envelope 完成了一次真实对话。第一行空 assistant 消息对应模型请求工具的步骤，它没有正文，只包含 tool_calls。

## 本章小结

- `PromptSection` / `PromptAssembler`：稳定 order 排序、同层重名拒绝、变量 provider 与严格替换
- `ToolRegistry`：注册查重、`schemas()` 说明书投影，模型接口与执行接口分离
- 循环改造：请求 envelope 的两半，组装出的 system 与注册表的 tools

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/core/system-prompt/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/README.zh.md) | `PromptAssembler` | 对齐有序 section、同层重名拒绝与每次 render 求值的具名 variable |
| 同上 | scope 与重名 | 跨 scope 最近层可以遮蔽远层；同一层重复 section 名称会报错 |
| 同上 | 排序确定性 | section 同 order 保持注册顺序；工具默认按名称排序，显式 `toolOrder` 才覆盖 |
| [`packages/core/tools/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/README.zh.md) | `ToolRegistry` | 对齐注册表与模型面 schema；执行器只留在本地，不进入请求 envelope |

官方比教学版多一块：工具 schema 属于组装结果本身。core/tools 文档写明注册表通过 `ctx.systemPrompt.tools()` 自动把工具 schema 送入系统提示词组装，模型获知自己能做什么是一个连贯整体，适配器再把 schema 作为独立 wire 字段传输。教学版把两者分开渲染，协议原生的 tools 字段直接交给 client，结构更直观，差异在练习 3 展开。

## 练习

1. **顺序实验。** 把规则段的 order 改成 −10，跑 demo 观察规则段是否排到人设段前面；再解释确定性排序对调试的价值：同一个 bug 为什么必须能稳定复现。
2. **同名拒绝。** 贡献两个同名段，观察第二次注册如何立刻失败；再解释为什么跨 scope 遮蔽与同层重名是两件不同的事。
3. **工具目录段。** 仿照官方，把 `registry.schemas()` 渲染成一段文本，作为 order 50 的段贡献给组装器。对比协议 tools 字段与写进提示词两种方式，模型是否还会按 JSON Schema 传参，token 消耗有什么差异。
4. **变量缺省。** 在 demo 里删掉 `variables={"name": "小算"}`，观察 render 如何在请求发出前直接报错；再用 `assembler.variable("name", lambda: "小算")` 修复。
