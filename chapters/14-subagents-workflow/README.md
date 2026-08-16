# 14｜Subagent：委派、隔离与并行

> 预计时间：60 分钟 ｜ 前置：完成第 07 章 ｜ 本章调用真实 DeepSeek 模型

一个 Agent 可以自己干完所有活，但有些任务天然适合**拆分**：
「同时查三份资料」「并行验证两个方案」——它们彼此独立，串行
干浪费时间，塞进一个 Agent 的上下文又互相干扰。官方的答案是
**Subagent（子智能体）**：把独立子任务委派给子 agent，父 agent
只负责拆任务、收结果、汇总。

官方把委派做成了一个**模型工具**（`tool-subagent` 文档第 5 行：
「基于一个已配置提供方、面向模型的委派工具」）——模型在需要
拆分时主动调用它，参数就是子任务描述。本章实现这套机制的教学版，
并讲透它最重要的两个设计：**上下文隔离**与**并行**。

## 14.1 原理：为什么子任务要「隔离」

先算一笔账。父 agent 跑了一小时的长任务，对话历史可能几万
token。现在要委派一个子任务「查一下这个函数的最新文档」。如果
子 agent 继承父的全部历史：

- 每次子任务请求都要背着几万 token 的父历史——**贵**；
- 父历史里的无关内容会干扰子 agent 的注意力——**乱**；
- 子 agent 的输出又混进父历史，越滚越大——**雪球**。

隔离的设计：子 agent 只拿到**一句自包含的 task 描述**，父历史
一个字都不带。代价是 task 必须写全（子 agent 没有上下文可依赖），
收益是每个子任务都从干净的小上下文开始。demo 第 ③ 节会亲眼
看到：子 agent 的完整消息只有 `system + task` 两条。

值得注意的一个官方例外：**fork**（`subagent-fork-in-process`）。
fork 子 agent 会继承父 agent 的已完成对话作为一次性种子——
官方文档说它是「in-process child seeded with the parent's
completed conversation turns」，适合「换个角度继续同一个问题」
的场景。教学版不实现 fork，第 14.6 节对照表里说明。

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

1. **全新 Session**：隔离的第一道墙。子 agent 的会话日志从
   `turn/start` 开始，只写 task 与自己的回复——父历史在物理上
   进不来。
2. **结构化的结果**：`SubagentResult` 区分 output 与 stop_reason，
   对应官方 `SubagentRun.result → { output, stopReason }`。为什么
   「部分文本」要单独保留？官方 :11 的原话——「被截断的回答不会
   被报告为成功，也绝不会被悄悄丢弃」。子 agent 跑到一半失败时，
   它已经生成的内容是有价值的线索，放进 output 一并交回父 agent。
3. **错误转结果**：异常不向上抛，转成 `stop_reason`——父 agent
   看到「子任务失败了，原因和部分结果如下」，下一轮自己决定
   重试还是换路。

## 14.3 并行：多个子任务同时跑

父 agent 一轮请求了两个 subagent 调用，串行执行的总耗时是两个
子任务之和。并行执行的正确姿势是线程池：

```python
def run_subagents_parallel(specs, client) -> list[SubagentResult]:
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        futures = [
            pool.submit(run_subagent, client, task, system_prompt)
            for task, system_prompt in specs
        ]
        return [future.result() for future in futures]
```

`DeepSeekClient.chat` 是同步阻塞调用（大部分时间在等网络），
线程池让多个等待同时进行——总耗时 ≈ 最慢的那个子任务。demo
第 ② 节的计时会证明这一点。官方对「并发安全」有更细的判定
（工具声明 `isConcurrencySafe`，只有安全的工具调用才并行，
文档见 core/tools），教学版简化为「subagent 调用天然可并行」。

## 14.4 委派工具：模型主动拆任务

把 `run_subagent` 包装成第 02 章风格的 Tool，模型就能在需要时
主动拆分：

```python
subagent_tool = Tool(
    name="subagent",
    description=(
        "把一个独立的子任务委派给子智能体执行并等待其完成。"
        "子智能体看不到当前对话历史，只看到 task 描述。"
        "适合：独立的小任务、并行探索多个方向。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要委派的子任务描述，要自包含（子智能体没有上下文）",
            }
        },
        "required": ["task"],
    },
    execute=execute,
)
```

注意 description 里那句「子智能体看不到当前对话历史，只看到
task 描述」——它**主动告诉模型**「写 task 时要自包含」。这是
隔离设计的配套：模型知道子 agent 没有上下文，写 task 时会把
必要信息写全。说明书（第 02 章的概念）在这里再次发挥威力。

## 14.5 跑一遍完整 demo

```bash
uv run python chapters/14-subagents-workflow/src/demo.py
```

真实输出（模型行为每次略有差异，结构稳定）：

```
=== ① 主 agent 请求并行委派 ===
  [主 agent → 请求工具] subagent({"task": "请计算 3+3 等于多少，并只返回计算结果。"})
  [主 agent → 请求工具] subagent({"task": "请计算 7*2 等于多少，并只返回计算结果。"})
  （本轮请求了 2 个 subagent，将并行执行）

=== ② 并行执行（计时） ===
  [子agent#1] 6  (completed)
  [子agent#2] 14  (completed)
  总耗时: 0.7s（若串行执行约为两倍）

=== ③ 上下文隔离证据 ===
  [system] 你是一个速算助手，直接给出答案。…
  [user] 请计算 3+3 等于多少，并只返回计算结果。…
  ← 父 agent 的对话历史一个字都没进来

=== ④ 主 agent 汇总 ===
  [主 agent] 两个任务都已计算完成，结果如下：
  - 3 + 3 = 6
  - 7 × 2 = 14
```

四节对应四个机制：① 模型收到「同时算两件事」的任务，主动
发出两个 subagent 调用（注意 task 写得自包含——description 的
引导生效了）；② 并行计时；③ 隔离的物理证据；④ 主 agent 把
两条 tool 结果回灌后汇总成最终回答。

## 14.6 本章小结：亲手写了什么

- `run_subagent`：独立 Session 的子 agent 循环、结构化结果、
  失败保留部分文本
- `run_subagents_parallel`：线程池并行
- `create_subagent_tool`：委派工具（description 引导自包含 task）
- 上下文隔离的物理机制与 token 账

## 14.7 对照官方 DSH

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/subagent/tool-subagent/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/subagent/tool-subagent/README.zh.md) | `create_subagent_tool` | 官方委派工具（第 5 行）；前台调用等待 run.result、失败保留部分文本（第 11 行）与本章一致 |
| [`packages/subagent/subagent/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/subagent/subagent/README.zh.md) | `run_subagent` | 官方服务 API：start 等待子 agent 发布（第 18 行）；官方支持多种提供方（进程内/进程外/远程），教学版只做进程内 |
| [`packages/subagent/subagent-fork-in-process/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/subagent/subagent-fork-in-process/README.zh.md) | （未实现） | 官方 fork：子 agent 继承父已完成对话为一次性种子（第 5 行）——教学版不实现，练习 3 探索 |
| [`packages/workflow/tool-workflow/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/workflow/tool-workflow/README.zh.md) | （练习 4） | 官方 workflow：JavaScript 编排脚本扇出 subagent——更大规模的多智能体编排 |

## 14.8 练习

1. **隔离的代价**：让父 agent 委派一个需要「上文」的任务（如
   「继续上面的重构」），观察子 agent 因为缺上下文而答非所问；
   再让父 agent 把必要上文写进 task 重试——体会「隔离」与
   「自包含 task」的配合。
2. **失败传播**：给子 agent 的 system_prompt 里加入「总是说
   我不知道」的设定，观察 SubagentResult 与主 agent 收到
   completed 但无意义的输出时的表现；再模拟子 agent 抛异常
   （如断网），观察 stop_reason=error 的路径。
3. **fork 语义**：读官方 fork 文档，设计一个 `fork=True` 参数：
   子 agent 的 Session 用父历史（截至最后一个 turn/end）初始化，
   实现并对比「隔离版」与「fork 版」的 token 消耗。
4. **workflow 探索**：读官方 workflow 文档，说明「脚本编排多
   subagent」相比「模型逐轮调用 subagent 工具」的优势与成本
   （提示：确定性、可复现、上限控制）。
