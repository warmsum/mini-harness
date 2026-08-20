# 13｜Goal、Plan Mode、用户问答与 Todo

> 预计时间：60 分钟 ｜ 前置：完成第 05 章（事件日志） ｜ 本章纯本地运行，不调用模型

第 07 章的 Agent 能连续对话，但连续不等于有目标。一个跨小时、跨会话的长任务，比如把内部工具迁移到新框架，需要回答三个问题：现在做到哪了？要不要继续？每一小步是什么？把答案存在对话里不可靠，模型会忘、会跑题；存在内存里不持久，崩溃就丢。

官方把长期状态和临时决策拆成不同能力：

- Goal：当前长任务的唯一目标，带生命周期状态机，active、paused、complete、blocked 四相，所有变更作为事件进会话日志；
- Plan Mode：当前 Session 是否处于“先调查和设计、暂不实施”的协作状态，最后一条 `plan/mode` 事件获胜；
- User Questions：独立的单 provider seam，模型工具或 Plan Mode 可以暂停并等待结构化人类答案；
- Todo：当前工作的任务清单，模型用整体替换的方式维护。

官方 goal 包将它定义为“事件溯源的同会话目标状态”。这与第 05 章的会话日志采用相同模型：当前状态由事件投影得到，日志是持久化依据。

## 学习目标

完成本章后，你将能够：

- 使用 active、paused、complete、blocked 表示 Goal 生命周期；
- 用 `GoalRef` 的 id 与 revision 拒绝基于旧状态的修改；
- 把目标变更写入会话日志，并通过重放恢复当前目标；
- 在 step 边界提交 Plan Mode 选择，并通过用户明确评审退出；
- 使用单 provider 用户问答 seam 传递问题与结构化答案；
- 使用整体替换的方式维护并校验 Todo 清单。

## 13.0 Plan Mode 与用户问答为什么分开

Plan Mode 是日志中的协作状态，不是文件权限。开启后，下一次 Prompt 会多一段“先调查和设计”的引导；真正能否写文件仍由第 10、11 章的 Sandbox 和审批策略决定。

`UserQuestionService` 则只负责“把结构化问题交给当前 UI provider，并等待答案”。它只允许一个 provider，拒绝空问题批次，调用方提供 Agent 时还要验证它是精确 live root；由其他 Agent 所有的 child 不能把父任务卡在无人回答的交互上。

`exit_plan_mode` 始终注册。它只在执行时校验当前模式，要求完整计划以 `#` 标题开头，并提交一个带 `plan-review` intent 的问题。只有唯一的 `Approve` 选择且没有 custom 文本才算批准。

批准后不会立刻追加 `plan/mode=false`。控制器先保存 pending 选择，下一次已接受的 step 边界再提交，因此同一 assistant 工具批次中的剩余调用仍使用原来的 Plan Mode Prompt。这也避免模式切换让工具 schema 发生变化。

## 13.1 原理：为什么目标需要状态机

Agent 连续跑了三个小时的长任务，中途可能发生这些事：

- 用户说先停一下，目标该暂停，还是销毁？
- 依赖的上游服务挂了，目标是失败，还是等待？
- 会话崩溃后从磁盘恢复，目标还活着吗，会自动继续吗？

每一件事都要求目标有明确的 phase（阶段），并且阶段之间的迁移有明确规则。官方定义了四个阶段与六个动词：

| 动词 | 效果 | 官方语义 |
|------|------|----------|
| `create` | 建立目标 | revision=1、phase=active、启用续行 |
| `edit` | 改目标文本 | 保留 phase、blocker reason 与 activation |
| `pause` / `resume` | 暂停 / 恢复 | 停用 / 恢复续行；resume 清除阻塞原因 |
| `complete` | 完成 | 停用续行 |
| `block` | 阻塞 | 记录文本说明，只用一个持久 phase |

两个关键的官方设计决策：

决策一，最多只有一个当前目标。不允许同时进行三个目标的模糊状态，长任务的推进逻辑要求此刻唯一要完成的事永远清楚。已完成的目标可以换新目标，但进行中的只能有一个。

决策二，续行启用状态不持久化。会话从崩溃中恢复后，即使日志中的目标 phase 仍是 active，Agent 也不会自动继续，必须显式 resume。恢复后的工作区和依赖可能已经变化，因此默认停下等待确认比自动续跑更安全。

## 13.2 GoalStore：动词与 revision 守卫

目标状态的载体：

```python
@dataclass(frozen=True)
class Goal:
    id: str
    revision: int
    phase: str
    objective: str
    max_rounds: int
    rounds_started: int = 0
    blocker_reason: str | None = None
```

每个动词都遵循同一个流程：校验、生成新快照、revision 加一、追加 goal/change 事件。以 resume 为例：

```python
    def resume(self, ref: GoalRef) -> GoalRef:
        current = self._require(ref)
        if current.rounds_started >= current.max_rounds:
            raise ValueError("目标轮次已达上限，无法 resume")
        self._commit(self._with_phase(current, PHASE_ACTIVE, None), "resume")
        return GoalRef(id=current.id, revision=current.revision + 1)
```

`_require` 使用 Compare-and-Swap 思路校验调用方持有的引用：

```python
    def _require(self, ref: GoalRef) -> Goal:
        if self._current is None:
            raise ValueError("没有当前目标")
        if self._current.id != ref.id:
            raise ValueError(f"引用指向不同的目标（ref={ref.id} != 当前={self._current.id}）")
        if self._current.revision != ref.revision:
            raise ValueError(
                f"陈旧的引用（ref r{ref.revision} != 当前 r{self._current.revision}），"
                "请重新 get() 后再操作"
            )
        return self._current
```

为什么每个动词都要带引用、检查 revision？看两个并发场景：Agent A 拿着 r3 的引用决定暂停，同时 Agent B 已经把目标推进到 r5。A 基于过期的状态做决定，pause 会覆盖 B 的进展。revision 守卫让基于旧状态的决策在提交时被响亮拒绝。官方也使用 `GoalRef { id, revision }` 作为比较并设置防护。

## 13.3 事件溯源与连续性回放

每个动词最后都做同一件事，`_commit` 追加事件：

```python
    def _commit(self, goal: Goal, operation: str) -> None:
        self._current = goal
        self._session.append(
            "goal/change",
            {"version": 1, "operation": operation, "goal": _goal_to_dict(goal)},
        )
```

`goal/change` 同时记录操作名和变更后的完整快照，于是目标状态与第 05 章的会话一样可回放。`clear()` 是个例外：它写入带下一 revision 的 tombstone，明确记录“这个目标被删除了”，而不是让状态凭空消失。

```python
    @classmethod
    def replay(cls, session: Session) -> "GoalStore":
        store = cls(session)
        for event in session.events:
            if event.type == "user/message":
                source = event.data.get("source")
                if isinstance(source, dict) and source.get("kind") == "goal":
                    # 校验 goal id、revision 与 round 连续后，
                    # 只推进 rounds_started。
                    ...
                continue
            if event.type == "goal/change":
                # 校验完整快照、revision 或 clear tombstone。
                ...
        return store
```

一个细节：`admit_round()` 追加的是带 goal source 的 `user/message`。round 是已接纳目标消息的投影，不是一次目标配置变更，因此 `rounds_started` 增加，revision 保持不变。revision 连续性只在同一目标内检查；每个新目标又从 r1 开始。

这里的“连续性回放”是教学子集：它校验 goal id、revision、round 和 clear tombstone 的连续关系，但没有实现官方 invariant 模块的完整形状校验、非法生命周期迁移校验与时间戳单调性检查。练习 2 会继续补生命周期迁移规则。

## 13.4 Todo：整体替换式任务清单

Goal 回答做到哪了、要不要继续，Todo 回答眼前这几步是什么。官方 todo_write 工具的语义很特别：每次调用都整体替换，模型发来完整列表，不存在部分更新：

```python
def todo_write(
    session: Session,
    items: list[TodoItem],
    *,
    allow_parallel_in_progress: bool,
) -> str:
    errors = validate_todos(
        items, allow_parallel_in_progress=allow_parallel_in_progress
    )
    if errors:
        return "Error: invalid todos: " + "; ".join(errors)
    snapshot = [{"content": item.content, "status": item.status} for item in items]
    session.append("todo/write", {"todos": snapshot})
    # ...返回统计文本
```

三个设计意图：

1. 整体替换让日志自洽。每个 `todo/write` 事件都是完整快照，回放时后写覆盖先写，UI 与恢复永远拿到一致状态，不存在增删了一半的中间态。
2. status 三值：pending、in_progress、completed，就三个。不加 blocked、waiting，状态越多模型越容易写错。
3. 严格校验：content 会先 trim，再检查空值和重复；非法 status 一律拒绝。`allow_parallel_in_progress=False` 时还会拒绝多个进行中条目。错误信息本身就是给模型的指导，模型下一轮会按提示修正。

## 13.5 运行完整示例

```bash
uv run python chapters/13-goal-plan-todo/src/demo.py
```

完整输出，本地确定性运行：

```
━━━ ① 用户问答 + Plan Mode ━━━
  provider 收到: 是否只迁移公开接口？
  ask_user_question → {"answers":[{"id":"scope","selected":["是"]}]}
  set(on) → committed
  provider 收到: Approve this plan and leave plan mode?
  exit_plan_mode → Plan approved — plan mode exited; carry out the plan starting with your next step.
  评审后: active=True, pending=False
  下一 step: active=False

━━━ ② Goal 生命周期 ━━━
  create →
  r1 [active] rounds=0/5  把内部工具迁移到新框架
  admit_round →
  r1 [active] rounds=1/5  把内部工具迁移到新框架
  pause →
  r2 [paused] rounds=1/5  把内部工具迁移到新框架
  resume →
  r3 [active] rounds=1/5  把内部工具迁移到新框架
  block →
  r4 [blocked] rounds=1/5  把内部工具迁移到新框架  (blocked: 依赖上游发布)
  resume（清除阻塞原因）→
  r5 [active] rounds=1/5  把内部工具迁移到新框架
  complete →
  r6 [complete] rounds=1/5  把内部工具迁移到新框架

━━━ ③ revision 守卫：过期引用被拒绝 ━━━
  引用指向不同的目标（ref=goal-4 != 当前=goal-11）

━━━ ④ 事件溯源：goal/change 事件与连续性回放 ━━━
  #4  goal/change create r1 [active]
  #6  goal/change pause r2 [paused]
  #7  goal/change resume r3 [active]
  #8  goal/change block r4 [blocked]
  #9  goal/change resume r5 [active]
  #10 goal/change complete r6 [complete]
  #11 goal/change create r1 [active]
  回放后的当前目标: r1 [active]
  ← 目标状态只由事件派生：日志是唯一持久权威

━━━ ⑤ todo：整体替换与校验 ━━━
  Updated todo list: 0 pending, 1 in progress, 0 completed.
  Updated todo list: 1 pending, 1 in progress, 0 completed.
  Error: invalid todos: 重复的 content: "写第 01 章"
  Error: invalid todos: 无效的 status: "doing"（只允许 pending/in_progress/completed）
```

观察点：① 先通过真实 `ask_user_question` 工具取得结构化答案，再通过始终注册的 `exit_plan_mode` 工具呈交计划；评审成功后先出现 pending，下一 step 才真正退出。② 里状态动词会推进 revision，`admit_round` 只推进 round；block 的阻塞原因在 resume 后被清除。③ 里拿着过期引用操作被响亮拒绝；⑤ 的两条错误信息分别要求去重和改正状态值。

## 13.6 进入 Capstone

第 17 章把 GoalStore、PlanModeController、UserQuestionService 和 Todo 绑定到同一个 Session。模型能调用 `get_goal`、`create_goal`、`update_goal`、`todo_write`、`ask_user_question` 与始终注册的 `exit_plan_mode`；Plan Mode 的 Prompt 段每个 step 重新渲染，批准后的 pending 选择也只在下一 step 边界提交。`--rpc` 另提供 `plan.set`，用于在 Agent 外部切换协作模式。

## 本章小结

- `Goal` 快照与四阶段状态机、六动词
- `UserQuestionService`：单 provider、稳定错误码、结构化答案与 live-root 边界
- `PlanModeController`：日志折叠、pending 选择、稳定 exit 工具与明确评审
- `GoalRef` 与 `_require`：id 与 revision 双重 Compare-and-Swap 守卫
- `goal/change` 完整快照、clear tombstone 与 goal 来源消息的连续性回放
- `todo_write`：整体替换、三值状态、trim 后去重与并行进行中策略
- 官方两个关键决策：单一目标、续行绝不持久化

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/goal/goal/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/goal/goal/README.zh.md) | `GoalStore` | 保留事件溯源、GoalRef 守卫、单一目标、六动词、完整快照和续行不持久化；回放校验只实现连续性子集 |
| 同上 | `admit_round` | 官方只有来源为 goal 且已准入的 `user/message` 才推进正数 Round；普通人类轮次不增加 `roundsStarted` |
| [`packages/todo/tool-todo/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/todo/tool-todo/README.zh.md) | `todo_write` | 对齐整体替换、完整快照、三值状态、内容校验与可配置的并行进行中策略 |
| [`packages/interaction/user-questions/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/interaction/user-questions/README.zh.md) | `UserQuestionService` | 对齐单 provider、空批次/错误 intent/live caller 校验；教学版同步阻塞 |
| [`packages/plan/plan-mode/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/plan/plan-mode/README.zh.md) | `PlanModeController` | 对齐最后事件获胜、边界提交、Prompt 状态与明确 plan review；未实现 `/plan` command projection |

Goal、Plan Mode 与 Todo 是三个维度：Goal 决定长任务是否继续，Plan Mode 决定当前如何协作，Todo 记录眼前步骤。它们不能互相替代。

## 练习

1. **并发冲突推演。** 纸笔推演两个并发操作，A 拿 r3 引用 pause，B 先 edit 到 r4，列出所有交错顺序，确认 revision 守卫如何拒绝陈旧写入。再把 B 换成 `admit_round`，解释为什么引用仍然有效。
2. **非法迁移。** 给 GoalStore 加非法迁移校验，比如 complete 之后不允许 pause，blocked 之后不允许 complete，对比官方 invariant 模块的做法，它在候选事件进入持久日志前拒绝。
3. **round 上限。** 把 max_rounds 设成 2，连续 admit_round 三次，观察 resume 的容量检查；讨论上限耗尽后官方要求人类做什么。
4. **评审拒绝。** 把 provider 改为返回 `Keep planning` 和 custom feedback，验证模式保持 active、反馈进入错误结果，并说明为什么 custom 文本不能与 Approve 一起视为批准。
