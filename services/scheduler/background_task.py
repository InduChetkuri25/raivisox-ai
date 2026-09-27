"""Background task management."""

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class TaskState(str, Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ScheduledTask:
    """A task scheduled for execution (data record used by the scheduler)."""

    id: str
    name: str
    task_type: str
    scheduled_at: float | None = None
    recurring_interval: int | None = None
    payload: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.payload is None:
            self.payload = {}


class BackgroundTask:
    """A background task that runs asynchronously."""

    def __init__(
        self,
        id: str,
        coro: Callable,
        *args,
        **kwargs,
    ):
        self.id = id
        self._coro = coro
        self._args = args
        self._kwargs = kwargs
        self.state = TaskState.PENDING
        self.result: Any = None
        self.error: str = ""
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self._task: asyncio.Task | None = None

    async def run(self) -> Any:
        self.state = TaskState.RUNNING
        self.started_at = time.time()
        try:
            self.result = await self._coro(*self._args, **self._kwargs)
            self.state = TaskState.COMPLETED
        except Exception as e:
            self.error = str(e)
            self.state = TaskState.FAILED
            self.result = None
        finally:
            self.completed_at = time.time()
        return self.result

    @property
    def duration(self) -> float:
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return 0.0


class BackgroundTaskManager:
    """Manages background task lifecycle."""

    def __init__(self):
        self._tasks: dict[str, BackgroundTask] = {}

    def register(self, task: BackgroundTask) -> None:
        self._tasks[task.id] = task

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def get_all(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def get_active(self) -> list[BackgroundTask]:
        return [t for t in self._tasks.values() if t.state == TaskState.RUNNING]

    def get_completed(self) -> list[BackgroundTask]:
        return [t for t in self._tasks.values() if t.state == TaskState.COMPLETED]

    def get_failed(self) -> list[BackgroundTask]:
        return [t for t in self._tasks.values() if t.state == TaskState.FAILED]

    async def execute(self, task_id: str) -> Any:
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")
        return await task.run()

    async def execute_all(self) -> dict[str, Any]:
        results = {}
        tasks = [self.execute(tid) for tid, t in self._tasks.items() if t.state == TaskState.PENDING]
        if tasks:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            pending_ids = [tid for tid, t in self._tasks.items() if t.state in (TaskState.PENDING, TaskState.COMPLETED, TaskState.FAILED)]
            for tid, outcome in zip(pending_ids, outcomes):
                results[tid] = outcome
        return results

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task and task._task and not task._task.done():
            task._task.cancel()
            task.state = TaskState.CANCELLED
            return True
        return False

    def status_summary(self) -> dict:
        return {
            "total": len(self._tasks),
            "pending": len([t for t in self._tasks.values() if t.state == TaskState.PENDING]),
            "running": len([t for t in self._tasks.values() if t.state == TaskState.RUNNING]),
            "completed": len([t for t in self._tasks.values() if t.state == TaskState.COMPLETED]),
            "failed": len([t for t in self._tasks.values() if t.state == TaskState.FAILED]),
            "cancelled": len([t for t in self._tasks.values() if t.state == TaskState.CANCELLED]),
        }
