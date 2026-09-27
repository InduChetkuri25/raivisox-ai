"""Task scheduling — one-time and recurring scheduled tasks."""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from services.scheduler.background_task import BackgroundTaskManager, TaskState


class ScheduleType(str, Enum):
    ONCE = "once"
    RECURRING = "recurring"
    CRON = "cron"


@dataclass
class ScheduledTask:
    """A task scheduled for execution."""

    id: str
    name: str
    task_type: str
    schedule_type: ScheduleType
    scheduled_at: float | None = None
    recurring_interval: int | None = None  # seconds
    cron_expression: str | None = None
    payload: dict = field(default_factory=dict)
    state: TaskState = field(default=TaskState.PENDING)
    result: Any = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    executed_at: float | None = None
    next_run: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "task_type": self.task_type,
            "schedule_type": self.schedule_type.value,
            "state": self.state.value,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "executed_at": self.executed_at,
            "next_run": self.next_run,
        }


@dataclass
class ScheduleResult:
    """Result of scheduling a task."""

    task_id: str
    scheduled: bool
    scheduled_for: float | None = None
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "scheduled": self.scheduled,
            "scheduled_for": self.scheduled_for,
            "message": self.message,
        }


class TaskScheduler:
    """Schedules tasks for one-time or recurring execution."""

    def __init__(self):
        self._scheduled: dict[str, ScheduledTask] = {}
        self._background = BackgroundTaskManager()

    def schedule_once(
        self,
        name: str,
        task_type: str,
        delay_seconds: float,
        payload: dict | None = None,
    ) -> ScheduleResult:
        task_id = f"sched_{int(time.time() * 1000)}"
        scheduled_at = time.time() + delay_seconds
        task = ScheduledTask(
            id=task_id,
            name=name,
            task_type=task_type,
            schedule_type=ScheduleType.ONCE,
            scheduled_at=scheduled_at,
            payload=payload or {},
            next_run=scheduled_at,
        )
        self._scheduled[task_id] = task
        return ScheduleResult(
            task_id=task_id,
            scheduled=True,
            scheduled_for=scheduled_at,
            message=f"Task scheduled for {delay_seconds:.1f}s from now",
        )

    def schedule_recurring(
        self,
        name: str,
        task_type: str,
        interval_seconds: int,
        payload: dict | None = None,
    ) -> ScheduleResult:
        task_id = f"sched_{int(time.time() * 1000)}"
        next_run = time.time() + interval_seconds
        task = ScheduledTask(
            id=task_id,
            name=name,
            task_type=task_type,
            schedule_type=ScheduleType.RECURRING,
            recurring_interval=interval_seconds,
            payload=payload or {},
            next_run=next_run,
        )
        self._scheduled[task_id] = task
        return ScheduleResult(
            task_id=task_id,
            scheduled=True,
            scheduled_for=next_run,
            message=f"Recurring task every {interval_seconds}s",
        )

    def get_scheduled(self, task_id: str | None = None) -> list[dict]:
        tasks = self._scheduled.values()
        if task_id:
            tasks = [t for t in tasks if t.id == task_id]
        return [t.to_dict() for t in tasks]

    def get_due(self) -> list[ScheduledTask]:
        now = time.time()
        return [
            t for t in self._scheduled.values()
            if t.next_run and t.next_run <= now and t.state == TaskState.PENDING
        ]

    def mark_executed(self, task_id: str, result: Any = None) -> None:
        task = self._scheduled.get(task_id)
        if task:
            task.state = TaskState.COMPLETED
            task.result = result
            task.executed_at = time.time()
            if task.schedule_type == ScheduleType.RECURRING and task.recurring_interval:
                task.state = TaskState.PENDING
                task.next_run = time.time() + task.recurring_interval

    @property
    def background_manager(self) -> BackgroundTaskManager:
        return self._background
