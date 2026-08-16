"""第 13 章 demo：目标生命周期 + todo 整体替换。

运行（无需 API，纯本地）：
    uv run python chapters/13-goal-plan-todo/src/demo.py

演示：
1. Goal 完整生命周期：create → admit_round → pause → resume → block → resume → complete
2. revision 守卫：拿着过期引用操作 → 拒绝
3. 事件溯源：goal/change 事件串 + 严格回放
4. todo：整体替换 + 校验（重复 content / 非法 status）
"""

from __future__ import annotations

from goal import GoalStore
from session import Session
from todo import STATUS_IN_PROGRESS, TodoItem, todo_write


def section(title: str) -> None:
    print(f"\n━━━ {title} ━━━")


def show_goal(store: GoalStore) -> None:
    goal = store.get()
    if goal is None:
        print("  （无目标）")
        return
    print(
        f"  r{goal.revision} [{goal.phase}] rounds={goal.rounds_started}/{goal.max_rounds}"
        f"  {goal.objective}"
        + (f"  (blocked: {goal.blocker_reason})" if goal.blocker_reason else "")
    )


def main() -> None:
    session = Session()
    store = GoalStore(session)

    section("① Goal 生命周期")
    ref = store.create("把内部工具迁移到新框架", max_rounds=5)
    print("  create → ")
    show_goal(store)
    show_goal(store)

    store.admit_round()  # 模拟一轮目标工作完成
    print("  admit_round → ")
    show_goal(store)
    show_goal(store)
    ref = store.get_ref()  # 轮次推进后旧引用已失效，重新取引用

    ref = store.pause(ref)
    print("  pause → ")
    show_goal(store)

    ref = store.resume(ref)
    print("  resume → ")
    show_goal(store)

    ref = store.block(ref, "依赖上游发布")
    print("  block → ")
    show_goal(store)

    ref = store.resume(ref)
    print("  resume（清除阻塞原因）→ ")
    show_goal(store)

    store.complete(ref)
    print("  complete → ")
    show_goal(store)

    section("② revision 守卫：过期引用被拒绝")
    try:
        store.create("第二个目标", max_rounds=3)
        stale_ref = ref  # 上一步 complete 之后的旧引用
        store.edit(stale_ref, "改文本")
    except ValueError as e:
        print(f"  {e}")

    section("③ 事件溯源：goal/change 事件与严格回放")
    for event in session.events:
        if event.type == "goal/change":
            data = event.data
            print(f"  #{event.id:<2} goal/change r{data['revision']} [{data['phase']}]")
    replayed = GoalStore.replay(session)
    current = replayed.get()
    print(f"  回放后的当前目标: r{current.revision} [{current.phase}]")
    print("  ← 目标状态只由事件派生：日志是唯一持久权威")

    section("④ todo：整体替换与校验")
    print("  " + todo_write(session, [TodoItem("写第 01 章", STATUS_IN_PROGRESS)]))
    print(
        "  "
        + todo_write(
            session,
            [
                TodoItem("写第 01 章", STATUS_IN_PROGRESS),
                TodoItem("写第 02 章", "pending"),
            ],
        )
    )
    print("  " + todo_write(session, [TodoItem("写第 01 章", "pending"), TodoItem("写第 01 章", "pending")]))
    print("  " + todo_write(session, [TodoItem("写第 01 章", "doing")]))


if __name__ == "__main__":
    main()
