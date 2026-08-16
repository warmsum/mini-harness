"""第 13 章：Goal —— 长任务的目标状态机。

对应官方 packages/goal/goal。核心语义（goal-goal/README.zh.md）：
1. 事件溯源：目标状态以 goal/change 事件进入会话日志，日志是唯一持久权威（:24）；
2. 单一目标：最多只有一个当前目标，revision 从 1 开始（:22）；
3. 动词集合：create / edit / pause / resume / complete / block；
4. 变更经 revision 比较并设置防护（Compare-and-Swap），拒绝陈旧引用（:20）；
5. 续行启用状态绝不持久化：会话恢复后必须显式 resume（:28）。
"""

from __future__ import annotations

from dataclasses import dataclass

from session import Session

PHASE_ACTIVE = "active"
PHASE_PAUSED = "paused"
PHASE_COMPLETED = "completed"
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
        revision=1、phase=active 的目标并启用续行（:22）。"""
        if self._current is not None and self._current.phase != PHASE_COMPLETED:
            raise ValueError("已有进行中的目标：必须先 complete 或 clear")
        goal = Goal(
            id=f"goal-{self._session.seq}",
            revision=1,
            phase=PHASE_ACTIVE,
            objective=objective,
            max_rounds=max_rounds,
        )
        self._commit(goal)
        return GoalRef(id=goal.id, revision=goal.revision)

    def edit(self, ref: GoalRef, objective: str) -> GoalRef:
        """编辑目标文本。官方：编辑保留 phase、blocker reason 与 activation（:22）。"""
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
        self._commit(goal)
        return GoalRef(id=goal.id, revision=goal.revision)

    def pause(self, ref: GoalRef) -> GoalRef:
        current = self._require(ref)
        self._commit(self._with_phase(current, PHASE_PAUSED, current.blocker_reason))
        return GoalRef(id=current.id, revision=current.revision + 1)

    def resume(self, ref: GoalRef) -> GoalRef:
        """恢复。官方：只有配置的 Round 上限仍有剩余容量时，resume 才接受
        已停止 phase 或 phase=active 但已停用续行的目标；清除 blocker reason（:22）。"""
        current = self._require(ref)
        if current.rounds_started >= current.max_rounds:
            raise ValueError("目标轮次已达上限，无法 resume")
        self._commit(self._with_phase(current, PHASE_ACTIVE, None))
        return GoalRef(id=current.id, revision=current.revision + 1)

    def complete(self, ref: GoalRef) -> GoalRef:
        current = self._require(ref)
        self._commit(self._with_phase(current, PHASE_COMPLETED, None))
        return GoalRef(id=current.id, revision=current.revision + 1)

    def block(self, ref: GoalRef, reason: str) -> GoalRef:
        """阻塞：记录策略代码与规范化文本说明（教学版只留文本）。
        官方：阻塞会停用续行，只记录一个持久 phase（:22）。"""
        current = self._require(ref)
        self._commit(self._with_phase(current, PHASE_BLOCKED, reason))
        return GoalRef(id=current.id, revision=current.revision + 1)

    def admit_round(self) -> GoalRef:
        """接纳一个目标轮次（官方：只有来源为 goal 且已准入的
        user/message 事件会推进正数 Round，:26）。
        与其他动词一致：返回新引用，旧引用随之失效。"""
        if self._current is None or self._current.phase != PHASE_ACTIVE:
            raise ValueError("没有 active 目标，无法接纳轮次")
        goal = Goal(
            id=self._current.id,
            revision=self._current.revision + 1,
            phase=self._current.phase,
            objective=self._current.objective,
            max_rounds=self._current.max_rounds,
            rounds_started=self._current.rounds_started + 1,
            blocker_reason=self._current.blocker_reason,
        )
        self._commit(goal)
        return GoalRef(id=goal.id, revision=goal.revision)

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

    def _commit(self, goal: Goal) -> None:
        """每次变更追加 goal/change 事件（携带变更后的完整快照，:24）。"""
        self._current = goal
        self._session.append("goal/change", _goal_to_dict(goal))

    @classmethod
    def replay(cls, session: Session) -> "GoalStore":
        """严格回放：只从 goal/change 事件派生状态。

        revision 连续性只在同一目标内检查——每个新目标（id 不同）
        的 revision 都从 1 重新开始（官方 :22 create 生成 revision=1）。"""
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


def _goal_to_dict(goal: Goal) -> dict:
    return {
        "id": goal.id,
        "revision": goal.revision,
        "phase": goal.phase,
        "objective": goal.objective,
        "max_rounds": goal.max_rounds,
        "rounds_started": goal.rounds_started,
        "blocker_reason": goal.blocker_reason,
    }


def _goal_from_dict(data: dict) -> Goal:
    return Goal(
        id=data["id"],
        revision=data["revision"],
        phase=data["phase"],
        objective=data["objective"],
        max_rounds=data["max_rounds"],
        rounds_started=data.get("rounds_started", 0),
        blocker_reason=data.get("blocker_reason"),
    )
