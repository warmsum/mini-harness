"""第 13 章 demo：Plan Mode、用户问答、Goal 与 todo。

运行（无需 API，纯本地）：
    uv run python chapters/13-goal-plan-todo/src/demo.py

演示：
1. 用户问答 seam 与 Plan Mode 的边界提交
2. Goal 完整生命周期和 revision 守卫
3. 事件溯源与回放
4. todo 整体替换与校验
"""

from __future__ import annotations

from goal import GoalStore
from plan import PlanModeController, make_exit_plan_mode_tool
from session import Session
from todo import STATUS_IN_PROGRESS, TodoItem, todo_write
from user_questions import (
    AnswerItem,
    QuestionAnswer,
    QuestionRequest,
    UserQuestionService,
    make_ask_user_tool,
)


class ApprovingProvider:
    def ask(self, request: QuestionRequest) -> QuestionAnswer:
        question = request.questions[0]
        print(f"  provider 收到: {question.question}")
        selected = (question.options[0].label,) if question.options else ()
        return QuestionAnswer((AnswerItem(question.id, selected),))


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

    section("① 用户问答 + Plan Mode")
    questions = UserQuestionService()
    questions.register_provider(ApprovingProvider())
    plan_mode = PlanModeController(
        session,
        "You are in plan mode. Explore and design before implementation.",
        questions,
    )
    ask_tool = make_ask_user_tool(questions)
    print(
        "  ask_user_question → "
        + ask_tool.execute(
            {
                "questions": [
                    {
                        "id": "scope",
                        "question": "是否只迁移公开接口？",
                        "options": [{"label": "是"}, {"label": "否"}],
                    }
                ]
            }
        )
    )
    print(f"  set(on) → {plan_mode.set(True)}")
    session.append("turn/start", {"turn": 1})
    exit_tool = make_exit_plan_mode_tool(plan_mode)
    print(
        "  exit_plan_mode → "
        + exit_tool.execute(
            {"plan": "# 迁移计划\n\n先检查公开接口，再实现，最后验证。"}
        )
    )
    print(f"  评审后: active={plan_mode.get().active}, pending={plan_mode.get().pending}")
    plan_mode.apply_boundary()
    print(f"  下一 step: active={plan_mode.get().active}")
    session.append("turn/end", {"turn": 1, "reason": "completed"})

    section("② Goal 生命周期")
    ref = store.create("把内部工具迁移到新框架", max_rounds=5)
    print("  create → ")
    show_goal(store)

    store.admit_round()  # 模拟一轮目标工作完成
    print("  admit_round → ")
    show_goal(store)
    ref = store.get_ref()  # admit_round 不推进 revision；取回当前引用便于继续演示

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

    section("③ revision 守卫：过期引用被拒绝")
    try:
        store.create("第二个目标", max_rounds=3)
        stale_ref = ref  # 上一步 complete 之后的旧引用
        store.edit(stale_ref, "改文本")
    except ValueError as e:
        print(f"  {e}")

    section("④ 事件溯源：goal/change 事件与连续性回放")
    for event in session.events:
        if event.type == "goal/change":
            data = event.data
            if data["operation"] == "clear":
                print(f"  #{event.id:<2} goal/change clear")
            else:
                goal = data["goal"]
                print(
                    f"  #{event.id:<2} goal/change {data['operation']} "
                    f"r{goal['revision']} [{goal['phase']}]"
                )
    replayed = GoalStore.replay(session)
    current = replayed.get()
    assert current is not None
    print(f"  回放后的当前目标: r{current.revision} [{current.phase}]")
    print("  ← 目标状态只由事件派生：日志是唯一持久权威")

    section("⑤ todo：整体替换与校验")
    print(
        "  "
        + todo_write(
            session,
            [TodoItem("写第 01 章", STATUS_IN_PROGRESS)],
            allow_parallel_in_progress=False,
        )
    )
    print(
        "  "
        + todo_write(
            session,
            [
                TodoItem("写第 01 章", STATUS_IN_PROGRESS),
                TodoItem("写第 02 章", "pending"),
            ],
            allow_parallel_in_progress=False,
        )
    )
    print(
        "  "
        + todo_write(
            session,
            [TodoItem("写第 01 章", "pending"), TodoItem("写第 01 章", "pending")],
            allow_parallel_in_progress=False,
        )
    )
    print(
        "  "
        + todo_write(
            session,
            [TodoItem("写第 01 章", "doing")],
            allow_parallel_in_progress=False,
        )
    )


if __name__ == "__main__":
    main()
