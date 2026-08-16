# 13｜Goal 与 Todo：长任务的持久状态机

> 预计时间：60 分钟 ｜ 前置：完成第 05 章（事件日志） ｜ 本章纯本地运行，不调用模型

第 07 章的 Agent 能连续对话，但「连续」不等于「有目标」。一个
跨小时、跨会话的长任务（比如「把内部工具迁移到新框架」）需要
回答三个问题：**现在做到哪了**？**要不要继续**？**每一小步
是什么**？把答案存在对话里不可靠——模型会忘、会跑题；存在
内存里不持久——崩溃就丢。

官方的答案是把这些「任务状态」升级成一等公民：

- **Goal**：当前长任务的唯一目标，带生命周期状态机（active /
  paused / completed / blocked），所有变更作为事件进会话日志；
- **Todo**：当前工作的任务清单，模型用「整体替换」的方式维护。

官方 `goal` 包的文档第一句定调：「事件溯源的同会话目标状态」——
Goal 与第 05 章的会话日志同一套哲学：**状态是事件的投影，日志
是唯一持久权威**。

## 13.1 原理：为什么目标需要状态机

设想 Agent 连续跑了三个小时的长任务，中途发生这些事：

- 用户说「先停一下」——目标该暂停，还是销毁？
- 依赖的上游服务挂了——目标是失败，还是等待？
- 会话崩溃后从磁盘恢复——目标还「活着」吗，会自动继续吗？

每一件事都要求目标有**明确的 phase（阶段）**，并且阶段之间的
迁移有明确规则。官方定义了五个阶段与六个动词：

| 动词 | 效果 | 官方语义（goal 文档 :22） |
|------|------|---------------------------|
| `create` | 建立目标 | revision=1、phase=active、启用续行 |
| `edit` | 改目标文本 | 保留 phase、blocker reason 与 activation |
| `pause` / `resume` | 暂停 / 恢复 | 停用 / 恢复续行；resume 清除阻塞原因 |
| `complete` | 完成 | 停用续行 |
| `block` | 阻塞 | 记录规范化文本说明，只用一个持久 phase |

两个关键的官方设计决策：

**决策一：最多只有一个当前目标（:22）。** 不允许「同时进行三个
目标」的模糊状态——长任务的推进逻辑要求「此刻唯一要完成的事」
永远清楚。已完成的目标可以换新目标，但进行中的只能有一个。

**决策二：续行启用状态绝不持久化（:28）。** 这是最反直觉也最
重要的一条。会话崩溃恢复后，即使日志里目标 phase 还是 active，
Agent **不会自动继续**——必须显式 `resume`。为什么？恢复后的
环境可能变了（工作区换了、依赖没了），自动续跑等于在无人看管
的状态下继续一个高风险长任务。安全默认是「停下等人确认」。

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

每个动词都遵循同一个流程：**校验 → 生成新快照（revision+1）→
追加 goal/change 事件**。以 resume 为例：

```python
    def resume(self, ref: GoalRef) -> GoalRef:
        current = self._require(ref)
        if current.rounds_started >= current.max_rounds:
            raise ValueError("目标轮次已达上限，无法 resume")
        self._commit(self._with_phase(current, PHASE_ACTIVE, None))
        return GoalRef(id=current.id, revision=current.revision + 1)
```

`_require` 是这套 API 的守卫核心——**Compare-and-Swap**：

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

为什么每个动词都要带引用、检查 revision？设想两个并发场景：
Agent A 拿着 r3 的引用决定「暂停」，同时 Agent B 已经把目标推进
到 r5——A 基于过期的状态做决定，pause 会覆盖 B 的进展。revision
守卫让「基于旧状态的决策」在提交时被响亮拒绝。官方 :20 的说法
是「变更以 GoalRef { id, revision } 作为比较并设置防护，并拒绝
陈旧引用」——同一个思想。

## 13.3 事件溯源与严格回放

每个动词最后都做同一件事——`_commit` 追加事件：

```python
    def _commit(self, goal: Goal) -> None:
        self._current = goal
        self._session.append("goal/change", _goal_to_dict(goal))
```

`goal/change` 事件携带**变更后的完整快照**（官方 :24「每次变更
都会追加持久的 goal/change 事件，其中携带变更后的完整快照」）。
于是目标状态与第 05 章的会话一样可回放：

```python
    @classmethod
    def replay(cls, session: Session) -> "GoalStore":
        store = cls(session)
        for event in session.events:
            if event.type != "goal/change":
                continue
            goal = _goal_from_dict(event.data)
            previous = store._current
            if (
                previous is not None
                and previous.id == goal.id
                and goal.revision != previous.revision + 1
            ):
                raise ValueError(f"goal/change revision 不连续: {goal.revision}")
            store._current = goal
        return store
```

一个容易踩的细节：revision 连续性只在**同一目标内**检查。每个
新目标的 revision 从 1 重新开始（官方 :22）——demo 第 ③ 节里
第一个目标走到 r7 完成后，第二个目标又从 r1 开始，回放必须
接受这个「重置」。

## 13.4 Todo：整体替换式任务清单

Goal 回答「做到哪了、要不要继续」，Todo 回答「眼前这几步是
什么」。官方 `todo_write` 工具的语义（tool-todo 文档第 5 行）
很特别：**每次调用都整体替换**——模型发来完整列表，不存在
部分更新：

```python
def todo_write(session: Session, items: list[TodoItem]) -> str:
    errors = validate_todos(items)
    if errors:
        return "Error: invalid todos: " + "; ".join(errors)
    snapshot = [{"content": item.content, "status": item.status} for item in items]
    session.append("todo/write", {"todos": snapshot})
    # ...返回统计文本
```

三个设计意图：

1. **整体替换 → 日志自洽**。每个 `todo/write` 事件都是完整快照，
   回放时「后写覆盖先写」（官方 :9），UI 与恢复永远拿到一致
   状态——不存在「增删了一半」的中间态。
2. **status 三值**：`pending` / `in_progress` / `completed`
   （官方 :11）。就三个，不加「blocked」「waiting」——状态越多
   模型越容易写错。
3. **严格校验**：空 content、重复 content、非法 status 一律拒绝
   （官方 :25「拒绝空或重复的 content」）——错误信息本身就是给
   模型的指导，模型下一轮会按提示修正。

## 13.5 跑一遍完整 demo

```bash
uv run python chapters/13-goal-plan-todo/src/demo.py
```

完整输出（本地确定性运行）：

```
━━━ ① Goal 生命周期 ━━━
  create →  r1 [active] rounds=0/5  把内部工具迁移到新框架
  admit_round →  r2 [active] rounds=1/5  ...
  pause →  r3 [paused] rounds=1/5  ...
  resume →  r4 [active] rounds=1/5  ...
  block →  r5 [blocked] rounds=1/5  ...  (blocked: 依赖上游发布)
  resume（清除阻塞原因）→  r6 [active] rounds=1/5  ...
  complete →  r7 [completed] rounds=1/5  ...

━━━ ② revision 守卫：过期引用被拒绝 ━━━
  陈旧的引用（ref r6 != 当前 r1），请重新 get() 后再操作

━━━ ③ 事件溯源：goal/change 事件与严格回放 ━━━
  #0  goal/change r1 [active]
  ...
  #7  goal/change r1 [active]     ← 第二个目标，revision 重新从 1 开始
  回放后的当前目标: r1 [active]
  ← 目标状态只由事件派生：日志是唯一持久权威

━━━ ④ todo：整体替换与校验 ━━━
  Updated todo list: 0 pending, 1 in progress, 0 completed.
  Updated todo list: 1 pending, 1 in progress, 0 completed.
  Error: invalid todos: 重复的 content: "写第 01 章"
  Error: invalid todos: 无效的 status: "doing"（只允许 pending/in_progress/completed）
```

观察点：① 里每次动词 revision 都 +1，block 的阻塞原因在 resume
后被清除；② 里拿着过期引用操作被响亮拒绝——防的是「基于旧状态
做决定」；④ 的两条错误信息各教模型一件事（去重、改正状态值）。

## 13.6 本章小结：亲手写了什么

- `Goal` 快照与五阶段状态机、六动词
- `GoalRef` + `_require`：id/revision 双重 Compare-and-Swap 守卫
- `goal/change` 事件溯源与严格回放（同目标内 revision 连续）
- `todo_write`：整体替换、三值状态、严格校验
- 官方两个关键决策：单一目标、续行绝不持久化

## 13.7 对照官方 DSH

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/goal/goal/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/goal/goal/README.zh.md) | `GoalStore` | 事件溯源（第 5 行）、单一目标与 revision=1（第 22 行）、goal/change 完整快照（第 24 行）、续行不持久化（第 28 行）、round 只由 goal 来源消息推进（第 26 行）与本章一一对应 |
| 同上（第 26 行） | `admit_round` | 官方「只有来源为 goal 且已准入的 user/message 事件会推进正数 Round；普通人类轮次绝不增加 roundsStarted」——教学版简化为显式调用 |
| [`packages/todo/tool-todo/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/todo/tool-todo/README.zh.md) | `todo_write` | 整体替换（第 5 行）、todo/write 快照事件（第 9 行）、三值状态（第 11 行）、校验（第 25 行） |

官方还有一块本章未展开：**plan mode**（`packages/plan/plan-mode`）——
修改文件前先出方案、经用户批准再动手的「计划审查」机制。它与
Goal/Todo 是不同维度（方案审查 vs 进度管理），留作练习 4 的
探索方向。

## 13.8 练习

1. **并发冲突推演**：纸笔推演两个并发操作（A 拿 r3 引用 pause、
   B 先 admit_round 到 r4）的所有交错顺序，确认每种顺序下
   revision 守卫的行为。
2. **非法迁移**：给 GoalStore 加「非法迁移」校验（如 completed
   之后不允许 pause、blocked 之后不允许 complete），对比官方
   invariant 模块的做法（提示：候选事件进入日志前拒绝）。
3. **round 上限**：把 max_rounds 设成 2，连续 admit_round 三次，
   观察 resume 的容量检查；讨论「上限耗尽后」官方要求人类做什么。
4. **plan mode 探索**：阅读官方 plan-mode 文档，设计一个简化的
   「改文件前先出方案，用户批准后才执行写工具」的机制，写出
   接口草图并说明与 Goal/Todo 的关系。
