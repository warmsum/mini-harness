"""第 14 章：owner 隔离的本地后台 Job 注册表。"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, replace
from threading import Event, Lock
from uuid import uuid4


@dataclass(frozen=True)
class JobSnapshot:
    id: str
    owner_id: str
    status: str
    output: str = ""
    diagnostic: str | None = None


class LocalJobs:
    """start/list/read/kill/wait；终态由首次结算决定。"""

    def __init__(self, max_concurrency: int = 4) -> None:
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or max_concurrency <= 0
        ):
            raise ValueError("max_concurrency 必须是正整数")
        self.max_concurrency = max_concurrency
        self._pool = ThreadPoolExecutor(max_workers=max_concurrency)
        self._lock = Lock()
        self._jobs: dict[str, JobSnapshot] = {}
        self._cancel: dict[str, Event] = {}
        self._futures: dict[str, Future[None]] = {}

    def start(
        self,
        owner_id: str,
        operation: Callable[[Event], str],
    ) -> JobSnapshot:
        with self._lock:
            active = sum(
                job.owner_id == owner_id
                and job.status in {"queued", "running", "stopping"}
                for job in self._jobs.values()
            )
            if active >= self.max_concurrency:
                raise RuntimeError(
                    f"owner 的后台 job 上限为 {self.max_concurrency}；"
                    "请先等待或取消现有 job"
                )
            job_id = f"job-{uuid4().hex}"
            snapshot = JobSnapshot(job_id, owner_id, "queued")
            cancel = Event()
            self._jobs[job_id] = snapshot
            self._cancel[job_id] = cancel
        future = self._pool.submit(self._run, job_id, operation, cancel)
        with self._lock:
            self._futures[job_id] = future
        return snapshot

    def list(self, owner_id: str) -> tuple[JobSnapshot, ...]:
        with self._lock:
            return tuple(job for job in self._jobs.values() if job.owner_id == owner_id)

    def read(self, owner_id: str, job_id: str) -> JobSnapshot:
        with self._lock:
            return self._owned(owner_id, job_id)

    def wait(
        self, owner_id: str, job_id: str, timeout: float | None = None
    ) -> JobSnapshot:
        with self._lock:
            self._owned(owner_id, job_id)
            future = self._futures.get(job_id)
        if future is not None:
            try:
                future.result(timeout=timeout)
            except TimeoutError:
                pass
        return self.read(owner_id, job_id)

    def kill(self, owner_id: str, job_id: str) -> JobSnapshot:
        with self._lock:
            current = self._owned(owner_id, job_id)
            if current.status in {"queued", "running"}:
                self._cancel[job_id].set()
                self._jobs[job_id] = replace(current, status="stopping")
            return self._jobs[job_id]

    def close(self) -> None:
        with self._lock:
            for job_id, event in self._cancel.items():
                current = self._jobs[job_id]
                if current.status in {"queued", "running"}:
                    self._jobs[job_id] = replace(current, status="stopping")
                    event.set()
        self._pool.shutdown(wait=True)

    def _run(
        self,
        job_id: str,
        operation: Callable[[Event], str],
        cancel: Event,
    ) -> None:
        with self._lock:
            current = self._jobs[job_id]
            if current.status == "stopping":
                self._jobs[job_id] = replace(current, status="cancelled")
                return
            self._jobs[job_id] = replace(current, status="running")
        try:
            output = operation(cancel)
        except Exception as error:  # noqa: BLE001 - job 边界把任意任务失败结算为状态
            if cancel.is_set():
                self._settle(job_id, "cancelled")
            else:
                self._settle(job_id, "failed", diagnostic=str(error))
        else:
            if cancel.is_set():
                self._settle(job_id, "cancelled")
            else:
                self._settle(job_id, "completed", output=output)

    def _settle(
        self,
        job_id: str,
        status: str,
        *,
        output: str = "",
        diagnostic: str | None = None,
    ) -> None:
        with self._lock:
            current = self._jobs[job_id]
            if current.status in {"completed", "failed", "cancelled"}:
                return
            self._jobs[job_id] = replace(
                current, status=status, output=output, diagnostic=diagnostic
            )

    def _owned(self, owner_id: str, job_id: str) -> JobSnapshot:
        job = self._jobs.get(job_id)
        if job is None or job.owner_id != owner_id:
            raise KeyError("job 不存在或不属于当前 owner")
        return job
