# 14｜Subagent、Jobs 与 Workflow

> 预计时间：60 分钟 ｜ 前置：完成第 07 章 ｜ 本章调用真实 DeepSeek 模型

一个 Agent 可以顺序处理所有工作，但相互独立的任务更适合拆分，例如同时查询多份资料或验证两个方案。串行执行会增加等待时间，把所有过程放进同一个上下文又会混入无关信息。Subagent（子 agent）机制允许父 agent 委派独立任务，再收集和汇总结果；Jobs 负责后台生命周期，Workflow 负责有上限的批量编排。

官方把委派做成一个面向模型的工具。模型在需要拆分时主动调用它，参数就是子任务描述；工具再把请求交给具名 provider。本章从隔离的一次性子任务出发，继续加入 fork、continuable 会话、owner 隔离的后台 job 和 Python Workflow 教学引擎。

## 学习目标

完成本章后，你将能够：

- 判断哪些任务适合委派给上下文隔离的子 agent；
- 用独立 `Session` 运行子任务并返回结构化结果；
- 使用线程池并行执行多个互不依赖的子任务；
- 只继承父会话最后一个完整 turn，创建 fork 子 agent；
- 继续投递消息或中断 continuable 子 agent；
- 使用 owner 隔离的 Jobs 和有并发/总量上限的 Workflow。

## 14.1 原理：为什么子任务要隔离

假设父 agent 已经积累了数万 token 的对话历史，现在只需要委派一个“查询某个函数最新文档”的子任务。如果子 agent 继承父 agent 的全部历史，会带来三个问题：

- 每次子任务请求都要重复发送数万 token，增加成本；
- 父历史中的无关内容会干扰子任务；
- 子任务过程继续写入同一份历史，会让上下文增长得更快。

采用上下文隔离后，子 agent 只接收一条自包含的 task 描述，不继承父历史。代价是 task 必须提供完成任务所需的信息；收益是每个子任务都从较小且相关的上下文开始。demo 第 ③ 节会展示子 agent 的完整输入，其中只有 system 与 task 两条消息。

官方有一个例外：fork。它创建一个进程内子 agent，以父 agent 已完成的对话轮次作为初始内容，适合换个角度继续同一个问题。本章实现同一条边界：只复制到最后一个 `turn/end`，当前开放 turn 整段排除，避免把尚未结算的 assistant/tool 配对带进子会话。

## 14.2 run_subagent：独立的小型 Agent

```python
@dataclass(frozen=True)
class SubagentResult:
    output: str
    stop_reason: str  # "completed" / "max-steps" / "error"
    diagnostic: str | None = None


def run_subagent(client, task, system_prompt, max_steps=3, session=None) -> SubagentResult:
    session = session or Session()  # 默认隔离，也可接收 fork/continuable seed
    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": task})

    partial = ""
    try:
        for _step in range(max_steps):
            reply = client.chat(
                [Message(role="system", content=system_prompt),
                 *session.derive_messages()]
            )
            event = {"content": reply.content, "tool_calls": []}
            if reply.reasoning_content:
                event["reasoning_content"] = reply.reasoning_content
            session.append("assistant/message", event)
            partial = reply.content or ""
            if reply.content:
                session.append("turn/end", {"turn": 1, "reason": "completed"})
                return SubagentResult(output=reply.content, stop_reason="completed")
        session.append("turn/end", {"turn": 1, "reason": "max-steps"})
        return SubagentResult(output=partial, stop_reason="max-steps")
    except Exception as error:
        session.append(
            "turn/end", {"turn": 1, "reason": "error", "message": str(error)}
        )
        return SubagentResult(
            output=partial, stop_reason="error", diagnostic=str(error)
        )
```

三个教学要点：

1. 默认全新 Session 是隔离的第一道墙。子 agent 的会话日志从 turn/start 开始，只写 task 与自己的回复，父历史在物理上进不来；completed、max-steps、interrupted 与 error 退出路径都会补上 turn/end。
2. 结构化的结果：SubagentResult 区分 output、stop_reason 与 diagnostic，对应官方 SubagentRun.result 的三个字段。被截断的回答不会被报告为成功，也不会被悄悄丢弃。子 agent 跑到一半失败时，已经生成的内容仍放在 output；provider 或运行时错误只放在 diagnostic，不能伪装成子 agent 说过的话。
3. 错误转结果：异常不向上抛，转成 `stop_reason="error"`，具体原因单独放进 diagnostic。父 agent 因而能区分失败类别、诊断信息与部分回答，再决定重试还是换路。

fork seed 由一个很小的日志切片函数生成：

```python
def fork_session(parent: Session) -> Session:
    last_turn_end = -1
    for index, event in enumerate(parent.events):
        if event.type == "turn/end":
            last_turn_end = index
    if last_turn_end < 0:
        return Session()
    return Session.from_log(parent.events[:last_turn_end + 1])
```

没有完整 turn 时返回空 Session。这里复制事件值，不共享父 Session 对象；父子后续追加互不影响。

## 14.3 并行：多个子任务同时跑

父 agent 一轮请求了两个 subagent 调用，串行执行的总耗时是两个子任务之和。并行执行用线程池：

```python
def run_subagents_parallel(specs, client) -> list[SubagentResult]:
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        futures = [
            pool.submit(run_subagent, client, task, system_prompt)
            for task, system_prompt in specs
        ]
        return [future.result() for future in futures]
```

DeepSeekClient.chat 是同步阻塞调用，大部分时间在等网络，线程池让多个等待同时进行，总耗时约等于最慢的那个子任务。demo 第 ② 节的计时会证明这一点。官方的工具调度器只并行执行声明为并发安全的调用；subagent 工具符合这一条件，结果仍按模型给出的顺序提交。教学版只实现这一种并行场景。

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

description 明确说明“子 agent 看不到当前对话历史，只看到 task 描述”，用于引导模型生成自包含的 task。这是上下文隔离的必要配套，也说明第 02 章中的工具说明书会直接影响模型如何使用工具。

## 14.5 continuable：同一个子 Session 继续对话

官方把生命周期和调度分成两个维度：

| 维度 | 选项 | 含义 |
|------|------|------|
| 生命周期 | `one-shot` | 一次任务、一个结果；可前台等待，也可作为后台 job 运行 |
| 生命周期 | `continuable` | 子 agent 有持久会话和独立 inbox，可以继续发消息；官方还支持冷恢复 |
| 调度 | foreground | 父 agent 等待结果，再继续当前 step |
| 调度 | background | 立即返回 job id 或 child id，父 agent 继续工作，结果稍后通过通知或查询获取 |

`one-shot` 默认前台，`continuable` 默认后台。两者不是同一个概念：一次性任务也能后台跑，可继续子 agent 也能暂时前台等。

教学版 `ContinuableSubagent` 保留一个独立 Session，并用单 worker 的 `ThreadPoolExecutor` 作为 FIFO inbox。`submit_message()` 在队列接收消息后立即返回 Future，即使 child 正在运行也能继续投递；`send_message()` 复用同一队列但同步等待这一条消息完成。`interrupt()` 设置当前 turn 的取消信号；同步模型请求无法被硬中止，因此运行器在调用前后检查信号，并把已知的部分输出与 `interrupted` 终态分开结算。

`SubagentManager` 以 owner id 保存 child。`get()`、`list()` 和后续消息都要求相同 owner，兄弟 root 不能只凭 child id 越权访问。关闭 manager 时会先中断并回收所有 child。教学版没有冷恢复、report 工具、provider 注册表、能力协商和委派深度，但 one-shot、fork、continuable 三条进程内路径已经可运行。

官方 one-shot 失败时把非 assistant 诊断限制为 4096 个 UTF-8 字节，并与部分 output 分开。教学版保留字段分离，但没有字节截断。官方 continuable child 还能通过 report 工具选择 `next-step` 唤醒或 `quiet` 注入，本章尚未实现这条反向投递。

## 14.6 Jobs：后台运行与首次终态

一次性后台任务不能只返回一个线程对象，父 Agent 需要稳定 id、状态与结果。`LocalJobs` 提供 `start`、`list`、`read`、`wait` 和 `kill`：

```python
job = jobs.start(owner_id, operation)
snapshot = jobs.wait(owner_id, job.id, timeout=1.0)
```

每个 job 都绑定 owner，读取、等待和取消都会重新做所有权检查。状态从 queued 进入 running；kill 先进入 stopping，生产方真正停稳后才结算为 cancelled，避免过早释放容量。completed、failed 或 cancelled 终态由第一次结算决定，后到的结果不能覆盖。

准入按 owner 统计 queued、running 与 stopping，达到上限时会在分配 id 和调用 operation 之前拒绝；终止历史不占容量。取消采用协作式 Event，operation 必须主动检查它，Python 线程不能被安全强杀。`close()` 会向全部 job 发取消信号并等待线程池回收。教学版没有官方 completion notice、保留期限、持久化或跨进程恢复。

## 14.7 Workflow：有上限的批量编排

模型逐轮决定“再启动一个子 agent”灵活但昂贵。已知的扇出任务更适合一次 Workflow：先确定输入与阶段，再按并发上限执行。

`WorkflowEngine.parallel()` 并发运行一组 thunk，结果保持输入顺序，单项失败投影为 `None`。`pipeline()` 让每个 item 连续跑完自己的 stages，不在每个 stage 之间设置全局 barrier，因此快项不必等待慢项才能进入下一阶段。`max_concurrency` 限制同时运行数，`max_agents` 限制一次 run 或 pipeline 的总启动量。

官方 rc.8 在 Worker Thread 中执行受限 JavaScript 脚本，并暴露 `agent`、`pipeline`、`parallel`、`phase` 和 `log` hooks。教学版直接运行 Python callable，只保留编排与上限语义；线程不是隔离或安全边界，绝不能执行不可信脚本。

## 14.8 运行完整示例

```bash
uv run python chapters/14-subagents-workflow/src/demo.py
```

真实输出，模型行为每次略有差异，结构稳定：

```
=== ① fork + continuable：继承完整轮次并继续投递 ===
  两次结果: 已处理：继续第一项 / 已处理：再处理第二项
  子会话用户消息: ['已完成的父任务', '继续第一项', '再处理第二项']
  owner 隔离: 其他 root 无法访问 child

=== ② Jobs：后台执行、等待与首次终态 ===
  job-…: completed, output=已完成

=== ③ Workflow：并发与逐项 pipeline ===
  parallel=['A', 'B'], pipeline=[2, 4]

=== ④ 主 agent 请求并行委派 ===
  [主 agent → 请求工具] subagent({"task": "请计算 3+3 等于多少，并给出结果。"})
  [主 agent → 请求工具] subagent({"task": "请计算 7*2 等于多少，并给出结果。"})
  （本轮请求了 2 个 subagent，将并行执行）

=== ⑤ 并行执行（计时） ===
  [子 agent#1] 6  (completed)
  [子 agent#2] 14  (completed)
  总耗时: 0.8s（若串行执行约为两倍）

=== ⑥ 上下文隔离证据 ===
  [system] 你是一个速算助手，直接给出答案。…
  [user] 请计算 3+3 等于多少，并给出结果。…
  ← 父 agent 的对话历史一个字都没进来

=== ⑦ 主 agent 汇总 ===
  [主 agent] 两个子任务都已完成，结果如下：

1. **3 + 3 = 6**
2. **7 × 2 = 14**
```

前三节不访问网络：① 直接展示 fork 只继承完整 turn、continuable 可以继续投递且 child 受 owner 隔离；② 展示后台 Job 的状态结算；③ 展示 Workflow 的并发与逐项 pipeline。后四节才调用真实 API：模型发出两个自包含的 subagent 调用，并行执行后把结果交还主 agent 汇总。这样每个本章源码模块都能从 demo 入口实际观察。

## 14.9 进入 Capstone

第 17 章沿用官方默认 bundle 的工具分工：`subagent` 创建 isolated one-shot/continuable child，`subagent_fork` 只创建继承到最后完整 turn 的 one-shot child；两者都能选择前后台。后台 one-shot 返回 `job_id`，交给 `job_output`、`job_list` 和 `job_kill` 结算。后台 continuable 则在 FIFO inbox 接收首条消息后立即返回 `child_id` 与 accepted，后续 `send_message` 也只确认投递，不等待 child 回答，因此不会额外创建普通 Job。`interrupt_agent` 控制 continuable child，`workflow` 执行有上限的多子任务扇出。所有 child 和 job 都绑定当前 root owner。

## 本章小结

- `run_subagent`：独立 Session 的子 agent 循环、结构化结果、失败诊断与部分文本分离
- `fork_session`：只继承到最后一个完整 turn 的日志前缀
- `ContinuableSubagent` / `SubagentManager`：持久子 Session、继续消息、中断与 owner 隔离
- `run_subagents_parallel`：线程池并行
- `create_subagent_tool`：委派工具，description 引导自包含 task
- `LocalJobs`：后台状态机、协作式取消和首次终态
- `WorkflowEngine`：parallel、逐项 pipeline、顺序结算和两层上限

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/subagent/tool-subagent/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/subagent/tool-subagent/README.zh.md) | `create_subagent_tool`、Capstone `subagent` | 对齐模型面委派、失败诊断与部分文本分离，以及前后台选择 |
| [`packages/subagent/subagent/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/subagent/subagent/README.zh.md) | `run_subagent`、`ContinuableSubagent` | 教学版实现进程内 one-shot 与 continuable；没有具名外部 provider 和冷恢复 |
| [`packages/subagent/subagent-fork-in-process/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/subagent/subagent-fork-in-process/README.zh.md) | `fork_session` | 对齐只继承父级最后一个完整 turn 前缀；教学版没有官方创建窗口内的能力过滤 |
| [`packages/subagent/subagent-codex/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/subagent/subagent-codex/README.zh.md) / [`subagent-claude-code`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/subagent/subagent-claude-code/README.zh.md) | （未实现） | 官方 provider 可委派给原生 Codex 或 Claude Code；二者都是隔离上下文的一次性运行 |
| [`packages/jobs/jobs-local/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/jobs/jobs-local/README.zh.md) | `LocalJobs` | 对齐 owner 隔离、查询/等待/取消与首次终态；教学版没有 notice、TTL 和持久化 |
| [`packages/workflow/tool-workflow/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/workflow/tool-workflow/README.zh.md) | `WorkflowEngine`、Capstone `workflow` | 对齐批量 agent 编排和上限；教学版使用 Python callable，不执行官方 Worker Thread JavaScript |

## 练习

1. **隔离的代价。** 让父 agent 委派一个依赖上文的任务，例如继续上面的重构，观察子 agent 缺少哪些必要信息；再把这些信息写进 task 后重试，比较两次结果，说明隔离为什么要求 task 自包含。
2. **失败传播。** 给子 agent 的 system_prompt 里加入总是回答我不知道的设定，观察 SubagentResult 与主 agent 收到 completed 但无意义的输出时的表现；再模拟子 agent 抛异常，比如断网，观察 `stop_reason="error"`、diagnostic 与部分 output 如何保持分离。
3. **fork 语义。** 在父 Session 尾部构造一个未闭合 turn，运行 `fork_session()`，证明 child 只继承此前完整 turn；再对比隔离版与 fork 版的 token 消耗。
4. **停止占位。** 启动一个忽略取消、稍后返回的 job，先 kill，确认状态是 stopping 且新 job 被容量门拒绝；让旧 operation 返回后再 wait，确认最终是 cancelled。
5. **workflow 上限。** 用三个 item、两个 stage 推演 pipeline 的启动计数，再把 `max_agents` 设为 5，验证它在创建线程前拒绝整批运行。
