# 17｜headless 组装

> 预计时间：45 分钟 ｜ 前置：完成前 16 章 ｜ 本章调用真实 DeepSeek 模型

前 16 章分别实现了可以独立运行的机制。本章把其中的核心模块组装成一个 Python 包，并提供统一入口：接收任务文本、运行 Agent、保存会话、输出最终答案，再用退出码表示是否完成。

官方将这种形态称为 headless 组合包。bundle/headless 文档将它定义为一次性任务组合包，不挂载 Host、HTTP server、Web runtime 或浏览器插件。调用方通过一条命令提交任务，等待运行结束并读取结果。

## 学习目标

完成本章后，你将能够：

- 把前面章节的自包含模块整理为可导入的 Python 包；
- 实现接收单个任务的 headless runner；
- 区分 stdout 结果、stderr 过程信息和进程退出码；
- 说明一次任务如何经过 inbox、循环、工具、日志和持久化。

## 17.1 组装：从章节代码到一个包

前 16 章采用自包含的教学目录，每个 demo 与模块位于同一目录，因此代码使用 `from client import ...` 这类扁平导入。整理为包后，模块需要改用 `from .client import ...` 这样的包内相对导入。核心逻辑不需要改变，说明各章的模块边界能够直接用于最终组装。

组装清单：

| 包内模块 | 来自 | 贡献 |
|----------|------|------|
| `client.py` | 第 01/02 章 | 流式客户端 + 工具调用消息模型 |
| `session.py` | 第 05 章 | 事件日志与消息投影 |
| `registry.py` / `prompt.py` | 第 06 章 | 工具注册表 + 提示词组装 |
| `agent.py` / `inbox.py` | 第 07 章 | 常驻循环与 inbox |
| `persistence.py` | 第 08 章 | JSONL 持久化 |
| `meter.py` | 第 09 章 | token 计量 |
| `calculator.py` | 第 02 章 | 计算器工具 |

一个组装时才暴露的真实问题：第 01 章的 load_api_key 用 parents[3] 定位项目根，那是章节形态下的层级。进了包，层级变了，硬编码的深度失效。组装的修正版改成向上逐级查找第一个带 Key 的 .env，对任何嵌套深度都成立。这个差异正是包与教学目录的本质区别：包的代码要被放在任何地方运行，不能假设自己住在哪。

## 17.2 入口：headless runner

包的入口 `__main__.py` 实现官方 headless runner 的核心语义：创建 Agent，把任务作为普通用户消息提交，等待完全停稳，持久化会话，把最后一条 assistant 文本写 stdout，再让最终轮次的原因决定退出码：

```python
def run_task(task: str, session_file: str = SESSION_FILE) -> tuple[str, bool]:
    agent = build_agent()
    meter = TokenMeter(context_window=100_000)

    agent.followup(task)
    session = agent.run()

    pressure = meter.pressure(meter.measure(_messages_of(session)))
    print(f"[meter] 上下文占用 {pressure.ratio:.1%}", file=sys.stderr)

    store = JsonlStore(session_file)
    store.save(session)
    print(f"[persist] 会话已保存到 {store.path}", file=sys.stderr)

    final_text = ""
    completed = False
    for message in session.derive_messages():
        if message.role == "assistant" and message.content:
            final_text = message.content
    for event in session.events:
        if event.type == "turn/end" and event.data.get("reason") == "completed":
            completed = True
    return final_text, completed
```

四个设计点，全部来自前面的章节：

1. 任务就是一条普通用户消息。入口不做任何特殊处理，任务与对话里任何一句话地位相同，这让第 07 章的循环、第 05 章的日志都无需为 headless 开特例。
2. stdout 只放最终答案。计量、持久化这类过程信息走 stderr。stdout 的契约是给调用脚本的结果，混入日志会让脚本无法解析。官方在成功运行时保持 stderr 为空，教学版放宽为过程信息走 stderr。
3. 退出码等于完成与否：最终 turn/end 完成则 0，否则 1。调用脚本据此判断成功失败，而不是解析输出文本。
4. 落盘在退出前。首次保存经临时文件原子发布，后续保存先验证磁盘是内存日志的前缀，再只追加新增事件并 `fsync`。它既不覆盖历史，也不在每次 step 后整文件重写。

## 17.3 组装后仍然成立的不变量

最终包不是把旧章节代码简单复制到一起。下面这些后来补齐的规则也必须保留：

- 流式响应只有收到 `[DONE]` 才算完整；连接静默中断时抛错，不能把半条回答记为完整消息。
- Inbox 在 turn 边界先领取整批 next-step，再领取一条 next-turn；step 边界一次领取整批 steer。模型刚完成时若又有 steer，就在当前 turn 继续下一个 step。
- 每次模型调用都由 `step/start` 与 `step/end` 包住；`request/header` 只在 system、模型配置或工具 schema 变化时追加。
- Session 事件与嵌套 data 都是不可变快照，并拒绝无法无损写入 JSON 的值。
- 工具名稳定排序，prompt 同层重名立即失败，未知变量立即失败。
- 崩溃恢复只截断末尾未换行片段；完整坏行会报错。恢复时依次补齐开放的 `tool/result`、`step/end` 和真实 turn 编号的 `turn/end`。

这些规则分别来自前面章节，但只有在 capstone 里一起成立，headless 输出才可信：一个缺失的 `[DONE]`、一个未闭合的 step 或一次覆盖式保存，都可能让调用方把不完整运行误判为成功。

## 17.4 运行完整示例

```bash
uv run python chapters/17-headless-capstone/src/demo.py
```

真实输出，模型回答每次不同：

```
[meter] 上下文占用 0.0%
[persist] 会话已保存到 …/session.jsonl
=== ① 组装清单：前 16 章各贡献了哪一块 ===
  client.py                    来自 第 01/02 章    流式客户端 + 工具调用消息模型
  session.py                   来自 第 05 章       事件日志与消息投影
  registry.py / prompt.py      来自 第 06 章       工具注册表 + 提示词组装
  agent.py / inbox.py          来自 第 07 章       常驻循环与 inbox
  persistence.py               来自 第 08 章       JSONL 持久化
  meter.py                     来自 第 09 章       token 计量
  calculator.py                来自 第 02 章       计算器工具

=== ② 用组装的包跑真实任务 ===
  [stdout] 1+2*3 = **7**

根据运算优先级，先算乘法 2×3=6，再算加法 1+6=7。
  [exit] 0（正常完成）

=== ③ 会话落盘并读回 ===
  读回 N 条事件（数量取决于模型是否调用工具）
```

开头两行 [meter] 与 [persist] 是 stderr 上的过程信息，先于 stdout 出现是因为 stderr 不缓冲，这也正是两路输出分开的原因。也可以直接用包的入口跑，在 src 目录下：

```bash
cd chapters/17-headless-capstone/src
uv run python -m mini_harness "1+2*3 等于几？"
```

## 17.5 全书回顾：完整运行流程

下图按一次任务的执行顺序连接前面章节的核心机制：

```mermaid
flowchart TB
    TASK[任务文本] --> INBOX[第07章 inbox]
    INBOX --> LOOP[第07章 常驻循环]
    LOOP --> STEP[step/start]
    STEP --> ENV[第06章 envelope 组装<br>提示词 + 工具清单]
    ENV --> CALL[第01/02章 模型调用]
    CALL -->|tool_calls| TOOLS[第02章 工具执行]
    TOOLS -->|结果回灌| STEP
    CALL -->|final text| END[step/end / turn/end]
    STEP --> LOG[第05章 仅追加事件日志]
    END --> LOG
    LOG --> METER[第09章 token 计量]
    METER -->|压力过高| COMPACT[第09章 压缩]
    COMPACT --> LOG
    LOG --> PERSIST[第08章 持久化]
    PERSIST --> OUT[stdout + 退出码]
```

除主路径外，还有两条独立的线：第 03/04 章的插件系统、第 10/11 章的沙箱与审批；第 12 到 16 章是各自独立的能力。每一块都能在官方 DeepSeek Harness 源码里找到对应物，每章末尾的对照官方小节就是地图。

## 本章小结

- 包组装：扁平导入改相对导入，load_api_key 的层级修正
- `run_task`：headless runner 四设计点，任务即消息、stdout 契约、退出码语义、退出前落盘
- 全书 17 章核心机制的完整关系图

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/bundle/headless/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/headless/README.zh.md) | `mini_harness` 包 | 对齐一次性任务、不挂载 Host/HTTP、任务即用户消息、先持久化、stdout 输出和退出码语义 |
| 官方 `dsh --profile headless "task"` | `python -m mini_harness "task"` | 官方的任务文本就是命令行参数，教学版同款 |

官方 headless 组合包在 dsh-base 上叠加 headless-runner 插件，并由 cordis 完成装配；教学版直接组合前 16 章的模块。官方会先 `flush` Session，再只汇总本次 runner 持有的事件区间；遇到最终 error 时把 code/message 写 stderr，成功时 stderr 为空。教学版新建空 Session，因此全量汇总与本次区间等价，但会额外把 meter 和持久化路径写到 stderr。两者实现形态不同，核心的一次性任务语义一致。

## 练习

1. **加一个工具。** 把第 15 章的 WebSearchClient 组装进包，给 DeepSeek Harness 最新版本是多少这类任务跑一遍，观察模型何时选择搜索工具。
2. **压缩接通。** 把第 09 章的 compact 接进 run_task 的循环前，压力超阈值时压缩历史再请求，用小 context_window 验证长任务能跑完。
3. **退出码契约。** 给 run_task 传一个必然失败的任务，比如让模型请求不存在的工具，观察退出码与 stderr 的差异；写一个 shell 脚本用退出码做分支。
4. **发布形态。** 阅读 pyproject.toml 的 [project.scripts]，把包改成 pip install -e . 可安装形态，实现 mini-harness 命令行；对比章节目录直接跑与安装后跑的差异。
