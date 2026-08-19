# 13｜Goal 与 Todo

> 预计时间：60 分钟 ｜ 前置：完成第 05 章（事件日志） ｜ 本章纯本地运行，不调用模型

第 07 章的 Agent 能连续对话，但连续不等于有目标。一个跨小时、跨会话的长任务，比如把内部工具迁移到新框架，需要回答三个问题：现在做到哪了？要不要继续？每一小步是什么？把答案存在对话里不可靠，模型会忘、会跑题；存在内存里不持久，崩溃就丢。

官方的答案是把这些任务状态升级成一等公民：

- Goal：当前长任务的唯一目标，带生命周期状态机，active、paused、complete、blocked 四相，所有变更作为事件进会话日志；
- Todo：当前工作的任务清单，模型用整体替换的方式维护。

官方 goal 包将它定义为“事件溯源的同会话目标状态”。这与第 05 章的会话日志采用相同模型：当前状态由事件投影得到，日志是持久化依据。

## 学习目标

完成本章后，你将能够：

- 使用 active、paused、complete、blocked 表示 Goal 生命周期；
- 用 `GoalRef` 的 id 与 revision 拒绝基于旧状态的修改；
- 把目标变更写入会话日志，并通过重放恢复当前目标；
- 使用整体替换的方式维护并校验 Todo 清单。

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

这里的“连续性回放”是教学子集：它校验 goal id、revision、round 和 clear
tombstone 的连续关系，但没有实现官方 invariant 模块的完整形状校验、非法
生命周期迁移校验与时间戳单调性检查。练习 2 会继续补生命周期迁移规则。

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
━━━ ① Goal 生命周期 ━━━
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

━━━ ② revision 守卫：过期引用被拒绝 ━━━
  引用指向不同的目标（ref=goal-0 != 当前=goal-7）

━━━ ③ 事件溯源：goal/change 事件与连续性回放 ━━━
  #0  goal/change create r1 [active]
  #2  goal/change pause r2 [paused]
  #3  goal/change resume r3 [active]
  #4  goal/change block r4 [blocked]
  #5  goal/change resume r5 [active]
  #6  goal/change complete r6 [complete]
  #7  goal/change create r1 [active]
  回放后的当前目标: r1 [active]
  ← 目标状态只由事件派生：日志是唯一持久权威

━━━ ④ todo：整体替换与校验 ━━━
  Updated todo list: 0 pending, 1 in progress, 0 completed.
  Updated todo list: 1 pending, 1 in progress, 0 completed.
  Error: invalid todos: 重复的 content: "写第 01 章"
  Error: invalid todos: 无效的 status: "doing"（只允许 pending/in_progress/completed）
```

观察点：① 里状态动词会推进 revision，`admit_round` 只推进 round；block 的阻塞原因在 resume 后被清除。② 里拿着过期引用操作被响亮拒绝，防的是基于旧状态做决定；④ 的两条错误信息各教模型一件事，去重、改正状态值。注意 Goal 的完成阶段叫 `complete`，Todo 条目的完成状态才叫 `completed`。

## 本章小结

- `Goal` 快照与四阶段状态机、六动词
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

官方还有一块本章未展开：plan mode。修改文件前先出方案、经用户批准再动手的计划审查机制。它与 Goal 与 Todo 是不同维度，方案审查对进度管理，留作练习 4 的探索方向。

## 练习

1. **并发冲突推演。** 纸笔推演两个并发操作，A 拿 r3 引用 pause，B 先 edit 到 r4，列出所有交错顺序，确认 revision 守卫如何拒绝陈旧写入。再把 B 换成 `admit_round`，解释为什么引用仍然有效。
2. **非法迁移。** 给 GoalStore 加非法迁移校验，比如 complete 之后不允许 pause，blocked 之后不允许 complete，对比官方 invariant 模块的做法，它在候选事件进入持久日志前拒绝。
3. **round 上限。** 把 max_rounds 设成 2，连续 admit_round 三次，观察 resume 的容量检查；讨论上限耗尽后官方要求人类做什么。
4. **plan mode 探索。** 阅读官方 plan-mode 文档，设计一个简化的改文件前先出方案、用户批准后才执行写工具的机制，写出接口草图并说明与 Goal 与 Todo 的关系。
