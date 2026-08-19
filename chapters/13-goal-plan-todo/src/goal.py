"""第 13 章：Goal —— 长任务的目标状态机。

对应官方 packages/goal/goal。核心语义（packages/goal/goal）：
1. 事件溯源：目标状态以 goal/change 事件进入会话日志，日志是唯一持久权威；
2. 单一目标：最多只有一个当前目标，revision 从 1 开始；
3. 动词集合：create / edit / pause / resume / complete / block；
4. 变更经 revision 比较并设置防护（Compare-and-Swap），拒绝陈旧引用；
5. 续行启用状态绝不持久化：会话恢复后必须显式 resume。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from session import Session

PHASE_ACTIVE = "active"
PHASE_PAUSED = "paused"
PHASE_COMPLETE = "complete"
PHASE_BLOCKED = "blocked"


@dataclass(frozen=True)
class Goal:
    """一个目标状态的完整快照。"""

    id: str
    revision: int
    phase: str
    objective: str
    max_rounds: int
    rounds_started: int = 0
    blocker_reason: str | None = None


@dataclass(frozen=True)
class GoalRef:
    """目标引用：变更操作的比较凭证（id + revision）。"""

    id: str
    revision: int


class GoalStore:
    """同会话目标状态：动词 + 事件溯源 + revision 守卫。"""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._current: Goal | None = None

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get(self) -> Goal | None:
        """当前目标（与内部状态脱离的冻结快照）。"""
        return self._current

    def get_ref(self) -> GoalRef:
        """当前目标的引用（变更操作的凭证）。"""
        if self._current is None:
            raise ValueError("没有当前目标")
        return GoalRef(id=self._current.id, revision=self._current.revision)

    # ------------------------------------------------------------------
    # 动词（每个动词 = 校验 → 新快照 → goal/change 事件）
    # ------------------------------------------------------------------

    def create(self, objective: str, max_rounds: int = 30) -> GoalRef:
        """创建目标。官方：最多只有一个当前目标；create 生成
        revision=1、phase=active 的目标并启用续行。"""
        if self._current is not None and self._current.phase != PHASE_COMPLETE:
            raise ValueError("已有进行中的目标：必须先 complete 或 clear")
        goal = Goal(
            id=f"goal-{self._session.seq}",
            revision=1,
            phase=PHASE_ACTIVE,
            objective=objective,
            max_rounds=max_rounds,
        )
        self._commit(goal, "create")
        return GoalRef(id=goal.id, revision=goal.revision)

    def edit(self, ref: GoalRef, objective: str) -> GoalRef:
        """编辑目标文本。官方语义保留 phase、blocker reason 与 activation。"""
        current = self._require(ref)
        goal = Goal(
            id=current.id,
            revision=current.revision + 1,
            phase=current.phase,
            objective=objective,
            max_rounds=current.max_rounds,
            rounds_started=current.rounds_started,
            blocker_reason=current.blocker_reason,
        )
        self._commit(goal, "edit")
        return GoalRef(id=goal.id, revision=goal.revision)

    def pause(self, ref: GoalRef) -> GoalRef:
        current = self._require(ref)
        self._commit(
            self._with_phase(current, PHASE_PAUSED, current.blocker_reason), "pause"
        )
        return GoalRef(id=current.id, revision=current.revision + 1)

    def resume(self, ref: GoalRef) -> GoalRef:
        """恢复。官方：只有配置的 Round 上限仍有剩余容量时，resume 才接受
        已停止 phase 或 phase=active 但已停用续行的目标；清除 blocker reason。"""
        current = self._require(ref)
        if current.rounds_started >= current.max_rounds:
            raise ValueError("目标轮次已达上限，无法 resume")
        self._commit(self._with_phase(current, PHASE_ACTIVE, None), "resume")
        return GoalRef(id=current.id, revision=current.revision + 1)

    def complete(self, ref: GoalRef) -> GoalRef:
        current = self._require(ref)
        self._commit(self._with_phase(current, PHASE_COMPLETE, None), "complete")
        return GoalRef(id=current.id, revision=current.revision + 1)

    def block(self, ref: GoalRef, reason: str) -> GoalRef:
        """阻塞：记录策略代码与规范化文本说明（教学版只留文本）。
        官方语义中阻塞会停用续行，只记录一个持久 phase。"""
        current = self._require(ref)
        self._commit(self._with_phase(current, PHASE_BLOCKED, reason), "block")
        return GoalRef(id=current.id, revision=current.revision + 1)

    def clear(self, ref: GoalRef) -> None:
        """清除当前目标，并用带 revision 的 tombstone 留下持久记录。"""
        current = self._require(ref)
        cleared = {"id": current.id, "revision": current.revision + 1}
        self._session.append(
            "goal/change", {"version": 1, "operation": "clear", "cleared": cleared}
        )
        self._current = None

    def admit_round(self) -> GoalRef:
        """接纳一个目标轮次（官方：只有来源为 goal 且已准入的
        user/message 事件会推进正数 Round）。
        轮次是 goal 来源消息的投影，不是 goal/change；因此它不推进 revision。"""
        if self._current is None or self._current.phase != PHASE_ACTIVE:
            raise ValueError("没有 active 目标，无法接纳轮次")
        next_round = self._current.rounds_started + 1
        if next_round > self._current.max_rounds:
            raise ValueError("目标轮次已达上限")
        self._session.append(
            "user/message",
            {
                "content": self._current.objective,
                "source": {
                    "kind": "goal",
                    "goal_id": self._current.id,
                    "revision": self._current.revision,
                    "round": next_round,
                },
            },
        )
        self._current = replace(self._current, rounds_started=next_round)
        return GoalRef(id=self._current.id, revision=self._current.revision)

    # ------------------------------------------------------------------
    # 内部：revision 守卫 + 事件提交 + 重放
    # ------------------------------------------------------------------

    def _require(self, ref: GoalRef) -> Goal:
        """Compare-and-Swap：引用的 id 必须一致、revision 必须等于当前
        revision，否则拒绝——陈旧引用说明调用方拿着过期的目标状态做决定。"""
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

    def _with_phase(self, goal: Goal, phase: str, blocker: str | None) -> Goal:
        return Goal(
            id=goal.id,
            revision=goal.revision + 1,
            phase=phase,
            objective=goal.objective,
            max_rounds=goal.max_rounds,
            rounds_started=goal.rounds_started,
            blocker_reason=blocker,
        )

    def _commit(self, goal: Goal, operation: str) -> None:
        """每次变更追加 goal/change 事件，并携带变更后的完整快照。"""
        self._current = goal
        self._session.append(
            "goal/change",
            {"version": 1, "operation": operation, "goal": _goal_to_dict(goal)},
        )

    @classmethod
    def replay(cls, session: Session) -> "GoalStore":
        """连续性回放：只从 goal/change 与 goal 来源消息派生状态。

        revision 连续性只在同一目标内检查——每个新目标（id 不同）
        的 revision 都从 1 重新开始（create 生成 revision=1）。教学版未实现
        官方 invariant 的完整形状、生命周期迁移和时间戳校验。"""
        store = cls(session)
        for event in session.events:
            if event.type == "user/message":
                source = event.data.get("source")
                if not isinstance(source, dict) or source.get("kind") != "goal":
                    continue
                current = store._current
                if (
                    current is None
                    or current.phase != PHASE_ACTIVE
                    or source.get("goal_id") != current.id
                    or source.get("revision") != current.revision
                    or source.get("round") != current.rounds_started + 1
                    or source["round"] > current.max_rounds
                ):
                    raise ValueError("goal round 不连续或引用了错误的目标")
                store._current = replace(current, rounds_started=source["round"])
                continue
            if event.type != "goal/change":
                continue
            operation = event.data.get("operation")
            previous = store._current
            if operation == "clear":
                cleared = event.data.get("cleared")
                if (
                    previous is None
                    or not isinstance(cleared, dict)
                    or cleared.get("id") != previous.id
                    or cleared.get("revision") != previous.revision + 1
                ):
                    raise ValueError("goal clear tombstone 无效")
                store._current = None
                continue
            raw_goal = event.data.get("goal")
            if not isinstance(raw_goal, dict):
                raise ValueError("goal/change 缺少完整 goal 快照")
            goal = _goal_from_dict(raw_goal)
            if (
                previous is not None
                and previous.id == goal.id
                and goal.revision != previous.revision + 1
            ):
                raise ValueError(f"goal/change revision 不连续: {goal.revision}")
            store._current = goal
        return store


def _goal_to_dict(goal: Goal) -> dict[str, Any]:
    return {
        "id": goal.id,
        "revision": goal.revision,
        "phase": goal.phase,
        "objective": goal.objective,
        "max_rounds": goal.max_rounds,
        "rounds_started": goal.rounds_started,
        "blocker_reason": goal.blocker_reason,
    }


def _goal_from_dict(data: dict[str, Any]) -> Goal:
    return Goal(
        id=data["id"],
        revision=data["revision"],
        phase=data["phase"],
        objective=data["objective"],
        max_rounds=data["max_rounds"],
        rounds_started=data.get("rounds_started", 0),
        blocker_reason=data.get("blocker_reason"),
    )
