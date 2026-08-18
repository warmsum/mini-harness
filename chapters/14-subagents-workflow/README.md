# 14｜Subagent 委派

> 预计时间：60 分钟 ｜ 前置：完成第 07 章 ｜ 本章调用真实 DeepSeek 模型

一个 Agent 可以自己干完所有活，但有些任务天然适合拆分：同时查三份资料、
并行验证两个方案。它们彼此独立，串行干浪费时间，塞进一个 Agent 的上下文
又互相干扰。官方的答案是 Subagent，子 agent：把独立子任务委派给子 agent，
父 agent 只负责拆任务、收结果、汇总。

官方把委派做成一个模型工具，tool-subagent 文档第 5 行写明：基于一个已配置
提供方、面向模型的委派工具。模型在需要拆分时主动调用它，参数就是子任务
描述。本章实现这套机制的教学版，并讲透它最重要的两个设计：上下文隔离与
并行。

## 14.1 原理：为什么子任务要隔离

先算一笔账。父 agent 跑了一小时的长任务，对话历史可能几万 token。现在要
委派一个子任务，查一下这个函数的最新文档。如果子 agent 继承父的全部历史：

- 每次子任务请求都要背着几万 token 的父历史，贵；
- 父历史里的无关内容会干扰子 agent 的注意力，乱；
- 子 agent 的输出又混进父历史，越滚越大，雪球。

隔离的设计：子 agent 只拿到一句自包含的 task 描述，父历史一个字都不带。
代价是 task 必须写全，子 agent 没有上下文可依赖；收益是每个子任务都从
干净的小上下文开始。demo 第 ③ 节展示物理证据：子 agent 的完整消息只有
system 与 task 两条。

官方有一个例外：fork。subagent-fork-in-process 文档第 5 行写明，fork 提供方
创建一个进程内子 agent，以父 agent 已完成的对话轮次作为初始内容，适合换个
角度继续同一个问题的场景。教学版不实现 fork，对照表里说明。

## 14.2 run_subagent：独立的小型 Agent

```python
@dataclass(frozen=True)
class SubagentResult:
    output: str
    stop_reason: str  # "completed" / "max_turns" / "error"


def run_subagent(client, task, system_prompt, max_turns=3) -> SubagentResult:
    session = Session()  # 全新的会话：隔离的第一道墙
    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": task})

    partial = ""
    try:
        for _turn in range(max_turns):
            reply = client.chat(
                [Message(role="system", content=system_prompt),
                 *session.derive_messages()]
            )
            session.append("assistant/message", {"content": reply.content, "tool_calls": []})
            partial = reply.content or ""
            if reply.content:
                session.append("turn/end", {"turn": 1, "reason": "completed"})
                return SubagentResult(output=reply.content, stop_reason="completed")
        return SubagentResult(output=partial, stop_reason="max_turns")
    except Exception as error:
        return SubagentResult(output=partial, stop_reason=f"error: {error}")
```

三个教学要点：

1. 全新 Session 是隔离的第一道墙。子 agent 的会话日志从 turn/start 开始，
   只写 task 与自己的回复，父历史在物理上进不来。
2. 结构化的结果：SubagentResult 区分 output 与 stop_reason，对应官方
   SubagentRun.result 的 output 与 stopReason。为什么部分文本要单独保留？
   官方第 11 行写明：被截断的回答不会被报告为成功，也绝不会被悄悄丢弃。
   子 agent 跑到一半失败时，它已经生成的内容是有价值的线索，放进 output
   一并交回父 agent。
3. 错误转结果：异常不向上抛，转成 stop_reason。父 agent 看到子任务失败
   了，原因和部分结果如下，下一轮自己决定重试还是换路。

## 14.3 并行：多个子任务同时跑

父 agent 一轮请求了两个 subagent 调用，串行执行的总耗时是两个子任务之和。
并行执行用线程池：

```python
def run_subagents_parallel(specs, client) -> list[SubagentResult]:
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        futures = [
            pool.submit(run_subagent, client, task, system_prompt)
            for task, system_prompt in specs
        ]
        return [future.result() for future in futures]
```

DeepSeekClient.chat 是同步阻塞调用，大部分时间在等网络，线程池让多个等待
同时进行，总耗时约等于最慢的那个子任务。demo 第 ② 节的计时会证明这一点。
官方对并发安全有更细的判定，工具声明 isConcurrencySafe，只有安全的工具
调用才并行，tool-subagent 文档第 32 行写明同级委派并发安全；教学版简化为
subagent 调用天然可并行。

## 14.4 委派工具：模型主动拆任务

把 `run_subagent` 包装成第 02 章风格的 Tool，模型就能在需要时主动拆分：

```python
subagent_tool = Tool(
    name="subagent",
    description=(
        "把一个独立的子任务委派给子 agent 执行并等待其完成。"
        "子 agent 看不到当前对话历史，只看到 task 描述。"
        "适合：独立的小任务、并行探索多个方向。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要委派的子任务描述，要自包含（子 agent 没有上下文）",
            }
        },
        "required": ["task"],
    },
    execute=execute,
)
```

description 里那句子 agent 看不到当前对话历史，只看到 task 描述，是主动
告诉模型写 task 时要自包含。这是隔离设计的配套：模型知道子 agent 没有
上下文，写 task 时会把必要信息写全。第 02 章的说明书概念在这里再次发挥
威力。

## 14.5 跑一遍完整 demo

```bash
uv run python chapters/14-subagents-workflow/src/demo.py
```

真实输出，模型行为每次略有差异，结构稳定：

```
=== ① 主 agent 请求并行委派 ===
  [主 agent → 请求工具] subagent({"task": "请计算 3+3 等于多少，并给出结果。"})
  [主 agent → 请求工具] subagent({"task": "请计算 7*2 等于多少，并给出结果。"})
  （本轮请求了 2 个 subagent，将并行执行）

=== ② 并行执行（计时） ===
  [子 agent#1] 6  (completed)
  [子 agent#2] 14  (completed)
  总耗时: 0.8s（若串行执行约为两倍）

=== ③ 上下文隔离证据 ===
  [system] 你是一个速算助手，直接给出答案。…
  [user] 请计算 3+3 等于多少，并给出结果。…
  ← 父 agent 的对话历史一个字都没进来

=== ④ 主 agent 汇总 ===
  [主 agent] 两个子任务都已完成，结果如下：

1. **3 + 3 = 6**
2. **7 × 2 = 14**
```

四节对应四个机制：① 模型收到同时算两件事的任务，主动发出两个 subagent
调用，task 写得自包含，description 的引导生效了；② 并行计时，两个子任务
的耗时几乎等于一个；③ 隔离的物理证据；④ 主 agent 把两条 tool 结果回灌后
汇总成最终回答。

## 本章小结

- `run_subagent`：独立 Session 的子 agent 循环、结构化结果、失败保留部分文本
- `run_subagents_parallel`：线程池并行
- `create_subagent_tool`：委派工具，description 引导自包含 task
- 上下文隔离的物理机制与 token 账

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/subagent/tool-subagent/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/subagent/tool-subagent/README.zh.md) | `create_subagent_tool` | 委派工具定义在第 5 行；失败保留部分文本在第 11 行；同级委派并发安全在第 32 行 |
| [`packages/subagent/subagent/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/subagent/subagent/README.zh.md) | `run_subagent` | 官方 start 等待提供方发布一次性子 agent 在第 18 行；提供方决定子 agent 在进程内、其他进程还是远程，教学版只做进程内 |
| [`packages/subagent/subagent-fork-in-process/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/subagent/subagent-fork-in-process/README.zh.md) | （未实现） | 官方 fork 以父已完成对话轮次为初始内容（第 5 行），教学版不实现，练习 3 探索 |
| [`packages/workflow/tool-workflow/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/workflow/tool-workflow/README.zh.md) | （练习 4） | 官方 workflow 用脚本编排扇出 subagent，更大规模的多 agent 编排 |

## 练习

1. **隔离的代价。** 让父 agent 委派一个需要上文的任务，比如继续上面的
   重构，观察子 agent 因为缺上下文而答非所问；再让父 agent 把必要上文
   写进 task 重试，体会隔离与自包含 task 的配合。
2. **失败传播。** 给子 agent 的 system_prompt 里加入总是回答我不知道的
   设定，观察 SubagentResult 与主 agent 收到 completed 但无意义的输出时
   的表现；再模拟子 agent 抛异常，比如断网，观察 stop_reason=error 的
   路径。
3. **fork 语义。** 读官方 fork 文档，设计一个 fork=True 参数：子 agent
   的 Session 用父历史截至最后一个 turn/end 初始化，实现并对比隔离版
   与 fork 版的 token 消耗。
4. **workflow 探索。** 读官方 workflow 文档，说明脚本编排多个 subagent
   相比模型逐轮调用 subagent 工具的优势与成本，从确定性、可复现、上限
   控制三个角度想。
