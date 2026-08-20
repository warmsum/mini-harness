# 17｜Headless Agent 完整组装

> 预计时间：90 分钟 ｜ 前置：完成第 01–16 章 ｜ 命令行任务调用真实 DeepSeek 模型

前 16 章把 Harness 拆成可以独立运行的小机制。本章回答最后一个问题：这些机制怎样按照 DeepSeek Harness“一切皆插件”的架构进入同一个真实 Agent，而不是只停留在互不相连的 demo 中。

Capstone 仍然是 headless 运行时。它不启动网页、桌面界面或 HTTP 服务；普通模式从命令行接收一个任务，完成后把最后一条 assistant 文本写到 stdout。需要进程外控制时，`--rpc` 在 stdin/stdout 上提供逐行 JSON-RPC，同样不打开端口。

## 学习目标

完成本章后，你将能够：

- 用 Context、Service 与 Bundle 组装一个插件化 Agent；
- 区分插件能力与模型工具，说明 Service Definition、Provider、Consumer 的职责；
- 说明第 10–16 章分别接入了哪个运行时 seam；
- 跟踪 checkpoint、retry、pruner 与 spill 在一次 step 中的准确位置；
- 区分一次性任务出口与持续 JSON-RPC 入口；
- 识别 Python 教学实现与官方 headless bundle 的边界。

## 17.1 运行入口

安装依赖后，可以直接运行一次任务：

```bash
uv run mini-harness "查看当前项目并总结入口"
```

也可以从本章源码目录运行：

```bash
PYTHONPATH=chapters/17-headless-capstone/src \
  uv run python -m mini_harness "查看当前项目并总结入口"
```

程序把进度信息写到 stderr，把最终回答写到 stdout。最后一条 `turn/end` 的 reason 为 `completed` 时退出码是 0，否则是 1；缺少任务时先打印用法并以 2 退出，不会提前读取 API Key。

JSON-RPC 使用另一条入口：

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"settings.get","params":{"namespace":"agent"}}' \
  | DEEPSEEK_API_KEY=... uv run mini-harness --rpc
```

`--rpc` 每读一行请求就写一行响应。当前公开 `settings.get`、`agent.run` 与 `plan.set` 三个方法；这是第 16 章 dispatcher 的 stdio transport，不是完整 Host/API Proxy。

## 17.2 一切皆插件

官方的 Cordis 内核不实现 Agent 业务能力，它只管理插件加载、服务依赖、事件和清理。模型、工具、Session、Prompt、Agent Loop、文件系统、Skill、调度和 UI 都由插件提供或扩展。这就是“一切皆插件”：内核保持很小，能力通过稳定接缝组合。

它不等于“一切皆模型工具”。`skill`、`ask_user_question` 和 `job_output` 是插件注册给模型调用的工具；LLM provider、Session、retry、checkpoint、pruner 和 spill 也是插件，但不会出现在工具 schema 中。Capstone 最终有 32 个 fiber、25 个服务和 24 个模型工具，这三个数量表达的是不同层次。

一个能力通常拆成三部分：Service Definition 规定消费者依赖什么；Provider 提供可替换实现；Consumer 把服务接到 Prompt、工具或事件边界。Python 教学版用稳定服务名加 class 或 Protocol 表达 Definition，例如 LLM 的 `ChatClient` Protocol。例如 `filesystem_provider` 提供文件系统服务，`filesystem_consumer` 注册 5 个工具和安全提示。卸载 consumer 后，这 5 个工具 schema 与提示词 effect 会一起消失；替换 provider 时，依赖它的 consumer 会先卸载，再用新服务重新启动。

## 17.3 Context、Bundle 与 build_agent

`build_agent()` 现在只做三件事：创建根 Context，挂载 Python Bundle，从服务表取得 Agent。

```python
ctx = Context()
ctx.plugin(
    headless_bundle,
    BundleConfig(
        settings_document=load_settings_document(),
        enable_console_questions=enable_console_questions,
        checkpoint_flush=checkpoint_flush,
    ),
)
return cast(Agent, ctx.require("agent"))
```

`headless_bundle` 是显式的 Python 插件清单。它先挂载 Settings、Session、Prompt、Tools 和 LLM provider，再挂载文件、Shell、Skill、Goal、Plan、Web、Subagent、Jobs、Workflow 与四个策略插件，最后提供 Agent。等待 `agent` 服务的用户问答、委派和 RPC consumer 会在服务出现后自动启动。

Agent 的构造器不再接收 retry、checkpoint、Plan Mode、meter、pruner、spill 或清理回调。循环只发布 `agent/pre-step`、`agent/prepare-request`、`llm/request`、`tools/execute` 与 `tools/post-execute` 等事件，策略插件监听对应边界。新增策略时不需要再修改 Agent Loop。

工具注册表、Prompt 段和 RPC 方法的 `register` 都返回 disposer。插件通过 `ctx.effect(...)` 收集 disposer；`agent.close()` 只销毁根 Context，由它逆序卸载整棵插件树。这个收尾路径同时关闭 continuable Subagent 和 LocalJobs，并移除工具、Prompt、RPC 与服务注册。

## 17.4 第 10–16 章接在哪里

Capstone 一共注册 24 个模型工具，包含 calculator 和下面 23 个第 10–16 章工具。

| 章节 | 工具或服务 | Capstone 接线 |
|---|---|---|
| 10 | `read`、`write`、`edit`、`grep`、`glob` | Filesystem provider + 工具/Prompt consumer |
| 11 | `shell` | Shell provider + 工具 consumer；教学版策略不是内核沙箱 |
| 12 | `skill` | SkillCatalog provider + 渐进加载工具/Prompt consumer |
| 13 | `get_goal`、`create_goal`、`update_goal`、`todo_write` | GoalTodo provider；状态写入 Session 服务 |
| 13 | `ask_user_question`、`exit_plan_mode` | Question/Plan provider + 等待 Agent 的交互 consumer |
| 14 | `subagent`、`subagent_fork`、`send_message`、`interrupt_agent` | Subagent provider + 委派 consumer |
| 14 | `job_output`、`job_list`、`job_kill` | owner 隔离的 LocalJobs provider |
| 14 | `workflow` | 有并发与总量上限的 Workflow provider |
| 15 | `web_search`、`web_fetch` | Web provider + 两个独立工具 |
| 16 | Settings、RpcDispatcher | Settings provider + 等待 Agent/Plan 的 RPC consumer |

全部 consumer active 时工具清单是固定的 24 项。Plan Mode 开关只改变 `plan:policy` Prompt 段，`exit_plan_mode` 在默认模式下也继续出现在 schema 中；若显式卸载某个工具 consumer，它贡献的 schema 会立即从下一次请求消失。

## 17.5 一次 step 的插件顺序

Agent 在每个 step 中只发布边界，插件按注册顺序完成下面的工作：

1. Checkpoint 插件先在 `agent/pre-step` 持久化上一个已提交批次；
2. Plan 插件再在同一事件中应用待生效的 `plan/mode` 选择；
3. 追加 `step/start`，接纳本 step 的用户消息与 Plan Mode narration；
4. 重新组装 system prompt，连同当前消息表层和工具 schema 交给 meter 计量；
5. pruner 插件在 `agent/prepare-request` 计量压力；达到 80% 时才扫描超过字符阈值的工具结果，并在发生替换后重新派生消息表层；
6. 记录发生变化的 request header；
7. `llm/request` waterfall 的外层是 retry，内层是 checkpoint；每次模型尝试都会先成功持久化；
8. 模型失败时 RetryPolicy 记录 `llm/retry`，通过 `checkpoint/retry` 持久化调度事件后才退避；等待结束记录 `llm/retry-started`，并在同一个 step 复用已接受的 assembly；
9. 对每个 `tool/call`，checkpoint 插件在 `tools/execute` waterfall 中成功持久化后才放行工具正文；
10. spill 插件在 `tools/post-execute` 检查最终纯文本结果，必要时保存原文并把预算内预览写入 `tool/result`，最后追加 `step/end`。

checkpoint 失败采用 fail-closed：模型适配器或工具正文不会越过失败屏障。它保证的是“执行意图先持久化”，不是任意外部副作用的恰好一次。

## 17.6 Plan Mode 与用户问答

`plan.set` 在 turn 之间立即追加 `plan/mode`。如果 Agent 正在一个开放 turn 中，选择先留在 pending 状态，到下一次已接受的 step 边界才提交。

JSON-RPC 的 `plan.set` 要求 `active` 是真正的 JSON boolean。字符串 `"false"` 不会按 Python truthiness 误转成 true，而是返回 `INVALID_PARAMS`。

`exit_plan_mode` 要求计划以 `#` 一级标题开头，并通过 UserQuestionService 提交结构化评审。只有唯一答案项、唯一选择 `Approve`、且没有 custom 文本时才算批准。

批准工具结果返回后，日志里的模式仍暂时是 active。同一 assistant 响应中剩余的工具调用继续受 Plan Mode 引导；下一 step 边界才写入 `plan/mode=false`。这与官方 rc.8 的批次语义一致。

普通 Headless CLI 的 provider 在 stderr 展示标题、计划和选项，从 stdin 读取编号、标签或自定义反馈。`--rpc` 模式不注册这个 Console provider，因为 JSON-RPC line transport 与 `input()` 不能争用同一个 stdin；当前最小 RPC 子集也没有单独的交互问答通道。用户问答 seam 只允许一个 provider，并验证精确 live root；被另一个 Agent 所有的 child 不应阻塞等待人类回答。

## 17.7 retry、checkpoint、pruner 与 spill

RetryPolicy 默认只重试 `EMPTY_RESPONSE`、`RATE_LIMIT`、`SERVER`、`TIMEOUT` 与 `TRANSPORT`，最多五次，采用 500ms 到 10s 的有界指数退避和 10% 对称 jitter。

有效且不超过上限的 `Retry-After` 会替代本地退避。每次计划等待前写 `llm/retry` 并执行 checkpoint，等待完成后写 `llm/retry-started`。空的 completed 响应也会归类为 `EMPTY_RESPONSE`。失败输出不进入 surface，同 step 的重试复用已接受的 Prompt assembly，不重复运行 pre-step 策略。

ToolResultPruner 按 Unicode code point 计数。meter 先测量 system、当前消息表层与工具 schema；只有总压力达到上下文窗口的 80%，pruner 才处理超过字符阈值的候选结果。发生剪枝时，它先写 `compaction/prune` shadow price，再保留固定 head、marker 与 tail，通过 append-only replacement 改写模型表层，完整原事件仍在日志中。

SpillPolicy 按 UTF-8 bytes 计数。过大的纯文本结果先交给 LocalSpillStore，模型只收到预算内的首尾预览、locator 和读取提示。Capstone 的 `read` 支持一基 `offset` 和最多 2000 行的 `limit`，因此可以按提示分页取回 spill 文件。保存失败、没有 backend、`read` 工具或嵌套结果都会保留原文，不能把一次成功工具调用改成失败。

第 09 章的模型摘要压缩仍作为独立教学流程运行。Capstone 已接入 meter、pruner 与 spill，但还没有把摘要器和 provider context-overflow recovery 完整装入 Agent loop。

## 17.8 fork、continuable、Jobs 与 Workflow

`subagent` 的 lifecycle 可选 `one-shot` 或 `continuable`，调度可选 foreground 或 background。`subagent_fork` 与官方默认 bundle 一样固定为 one-shot，另行选择前后台，不把 continuable 的 report/prompt 前缀插到继承历史之前。

isolated 子 Agent 从空 Session 开始，只看到自包含 prompt。fork 子 Agent 只复制父 Session 到最后一个完整 `turn/end` 的前缀，当前未闭合 turn 整段排除。

continuable 子 Agent 保留自己的 Session，并用单 worker FIFO 接收消息。后台创建在首条 prompt 被队列接收后立即返回 `child_id` 与 accepted；`send_message` 即使在 child 运行中也能继续投递，并同样只返回投递确认，不等待回答或创建 `job_id`。`interrupt_agent` 设置当前 turn 的取消信号。兄弟 child 和其他 root 不能用 id 越权访问。

后台 one-shot 执行进入 LocalJobs。每个 job 绑定 owner，支持 list/read/wait/kill；queued、running 和 stopping 共同占用 owner 容量。kill 先进入 stopping，生产方停稳后才成为 cancelled，终态仍由第一次结算决定。continuable child 不进入普通 Job；教学版也没有官方的 settlement notice、report 反向投递和冷恢复，因此父 Agent 要获得 child 的最终回答还需要扩展查询或通知接缝。

教学版 WorkflowEngine 用 Python 线程运行 callable，并提供 parallel 与逐项 pipeline 的并发和总量上限。官方 rc.8 在 Worker Thread 中执行受限 JavaScript 脚本，提供 `agent`、`pipeline`、`parallel`、`phase` 和 `log` hooks。教学版保留编排语义，但线程不是隔离或安全边界，模型面的 `workflow` 参数也简化为 `tasks[]`。

## 17.9 Settings

默认配置位于 `agent` namespace，解析顺序仍是 schema defaults < composition base < user document。

用户文档默认读取 `.mini-harness/settings.json`，例如：

```json
{
  "agent": {
    "sandbox_mode": "workspace-write",
    "shell_mode": "read-only",
    "approval_policy": "never",
    "retry_max_retries": 3,
    "spill_max_inline_bytes": 8192,
    "jobs_max_concurrency": 4,
    "workflow_max_agents": 16
  }
}
```

Settings 更新使用 revision 做 Compare-and-Swap。本章启动时读取一次配置；没有实现官方 Host 的热更新、配置持久化 RPC 和多 profile 动态重组装。

## 17.10 会话文件与退出

普通任务先分配唯一的 `.mini-harness/sessions/<uuid>.jsonl`。runner 将同一个 JsonlStore 的 `save` 作为 Bundle 配置交给 checkpoint 插件，所以请求与工具意图会在运行中逐步落盘。无论任务成功还是抛错，runner 都会在关闭根 Context 前执行最终 flush，保证 `step/end` 与 `turn/end(error)` 不只停留在内存中。stdio RPC 退出时也执行相同收尾。

默认 spill 文件位于 `.mini-harness/spills/<session>/`。suggested name 只作为安全文件名提示，backend 会生成碰撞安全名称并返回绝对 locator。

程序从 Session 派生最后一条非空 assistant 文本。完成状态只读取最后一条 `turn/end`，不能因为更早的 turn 成功就把后续错误误报为完成。

## 对照官方 rc.8

本章的官方源码对照版本是 tag `dsh-v0.1.0-rc.8`、commit `141eb6fef83422698aef7a981029e843e8161534`。

理解这套组合方式时，可以同时对照官方文档的 [Cordis Primer](https://deepseek-harness.github.io/deepseek-harness/reference/cordis-primer)、[Capability Seams](https://deepseek-harness.github.io/deepseek-harness/reference/capability-seams) 与 [Extension Cookbook](https://deepseek-harness.github.io/deepseek-harness/reference/cookbook/extension-cookbook)。它们分别解释插件内核、Definition/Provider/Consumer 接缝和新增能力应接到扩展点而不是修改 Agent Loop 的原则。

| 官方源码 | 教学版对应 | 边界 |
|---|---|---|
| `packages/bundle/base/cordis.patch.yml`、`packages/bundle/headless/cordis.patch.yml` | `cordis.py`、`bundle.py`、`build_agent` | Python 显式 Bundle 清单，没有 YAML Loader/HMR/isolate |
| `vendor/cordis/src/context.ts`、`fiber.ts`、`reflect.ts`、`events.ts` | `Context`、`PluginHandle`、`depends`、`waterfall` | 保留生命周期、依赖后到、服务替换与 effect 清理；同步教学版不实现异步 fiber |
| `packages/plan/plan-mode`、`packages/interaction/user-questions` | `plan.py`、`user_questions.py` | 保留日志折叠、稳定工具、评审和边界提交 |
| `packages/session/session-checkpoint-policy` | `checkpoint.py` 与 Agent 的请求、工具、pre-step、pre-retry flush 点 | 前三个边界对齐官方；教学版没有后台 batching controller，所以额外显式持久化 retry 调度事件 |
| `packages/llm/llm-retry` | `retry.py` | 实现 normal 有限策略；未实现 always 和 AbortSignal |
| `packages/compaction/compaction-tool-result-pruner` | `pruner.py` | 教学消息只有纯文本 block |
| `packages/spill/*` | `spill.py` | local provider + policy；未实现 dispatch-log 分支 |
| `packages/subagent/*`、`packages/jobs/jobs-local` | `subagent.py`、`jobs.py` | 进程内线程实现，无远程 provider 和冷恢复 |
| `packages/workflow/workflow-worker-thread` | `workflow.py` | Python callable 教学引擎，不是 Worker Thread 安全边界 |
| `packages/api/gateway` | `rpc.py`、`--rpc` | 最小 JSON-RPC 子集，无 Host/API Proxy |

## 本章小结

- 第 10–16 章已经通过 provider、consumer 与策略插件进入同一个 Capstone；
- Agent Loop 不直接持有 retry、checkpoint、Plan、pruner 或 spill，`build_agent()` 只挂载 Bundle；
- Plan Mode 与用户问答通过独立 seam 协作，模式只在 step 边界切换；
- checkpoint、retry、pruner 与 spill 位于不同边界，解决不同故障；
- fork、continuable Subagent、Jobs 与 Workflow 分别负责 seed、生命周期、后台结算和批量编排；
- headless CLI 支持一次性任务与 stdio JSON-RPC，但不冒充完整 Host 或平台沙箱。

## 练习

1. 卸载 `filesystem_consumer`，验证五个文件工具 schema 和对应 Prompt 段一起消失，再重新安装插件。
2. 卸载默认 LLM provider 并提供一个 FakeClient，观察 Agent consumer 如何自动重启，而 Agent Loop 无需修改。
3. 把第 09 章摘要器做成 `agent/prepare-request` 插件，验证缩小失败不会破坏原历史。
4. 为 `--rpc` 增加 `job.list` 与 `session.events` 方法，并保持错误响应的 request id。
