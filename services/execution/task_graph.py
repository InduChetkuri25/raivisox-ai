"""Task graph and parallel execution engine."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
import asyncio
import time


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskNode:
    id: str
    title: str
    description: str = ""
    status: ExecutionStatus = field(default_factory=lambda: ExecutionStatus.PENDING)
    result: Any = None
    error: str = ""
    depends_on: list[str] = field(default_factory=list)
    can_parallel_with: list[str] = field(default_factory=list)
    started_at: float = 0.0
    completed_at: float = 0.0

    @property
    def duration(self) -> float:
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return 0.0


class TaskGraph:
    """A directed acyclic graph of task nodes with dependency tracking."""

    def __init__(self):
        self.nodes: dict[str, TaskNode] = {}
        self._completed: set[str] = set()
        self._failed: set[str] = set()

    def add_node(self, node: TaskNode) -> None:
        self.nodes[node.id] = node

    def add_step(
        self,
        id: str,
        title: str,
        description: str = "",
        depends_on: list[str] | None = None,
        can_parallel_with: list[str] | None = None,
    ) -> TaskNode:
        node = TaskNode(
            id=id,
            title=title,
            description=description,
            depends_on=depends_on or [],
            can_parallel_with=can_parallel_with or [],
        )
        self.add_node(node)
        return node

    def get_ready_nodes(self) -> list[TaskNode]:
        """Return nodes whose dependencies are all completed."""
        ready = []
        for node in self.nodes.values():
            if node.status != ExecutionStatus.PENDING:
                continue
            if all(dep in self._completed for dep in node.depends_on):
                ready.append(node)
        return ready

    def get_parallel_groups(self) -> list[list[TaskNode]]:
        """Group nodes into parallel execution batches (topological levels)."""
        groups = []
        remaining = set(self.nodes.keys())
        # Seed with nodes completed in previous runs so a partial re-run
        # (retry) sees their dependencies as satisfied.
        completed = set(self._completed)

        while remaining - completed:
            ready = [
                nid
                for nid in (remaining - completed)
                if nid in self.nodes
                and self.nodes[nid].status == ExecutionStatus.PENDING
                and all(d in completed for d in self.nodes[nid].depends_on)
            ]
            if not ready:
                break
            groups.append([self.nodes[nid] for nid in ready])
            completed.update(ready)

        return groups

    @property
    def all_completed(self) -> bool:
        return all(n.status == ExecutionStatus.COMPLETED for n in self.nodes.values())

    @property
    def any_failed(self) -> bool:
        return any(n.status == ExecutionStatus.FAILED for n in self.nodes.values())

    @property
    def total_duration(self) -> float:
        if not self.nodes:
            return 0.0
        starts = [n.started_at for n in self.nodes.values() if n.started_at]
        ends = [n.completed_at for n in self.nodes.values() if n.completed_at]
        if starts and ends:
            return max(ends) - min(starts)
        return 0.0

    def to_dict(self) -> dict:
        return {
            "nodes": [
                {
                    "id": n.id,
                    "title": n.title,
                    "status": n.status.value,
                    "result": n.result,
                    "error": n.error,
                    "depends_on": n.depends_on,
                    "duration": n.duration,
                }
                for n in self.nodes.values()
            ],
            "all_completed": self.all_completed,
            "any_failed": self.any_failed,
            "total_duration": self.total_duration,
        }


class ParallelExecutor:
    """Executes task graph nodes with parallel batching and dependency resolution."""

    def __init__(self, max_concurrent: int = 4, on_node_transition=None):
        self.max_concurrent = max_concurrent
        self.graph = TaskGraph()
        self._step_fns: dict[str, tuple[Callable, tuple, dict]] = {}
        self._results: dict[str, Any] = {}
        self._errors: dict[str, str] = {}
        # Optional async callback fired at REAL node state transitions:
        # on_node_transition(node, transition) where transition is one of
        # "started" | "completed" | "failed" | "skipped". Used by the API
        # layer to persist step-level progress events; never fabricated.
        self.on_node_transition = on_node_transition

    async def _fire(self, node: "TaskNode", transition: str) -> None:
        cb = self.on_node_transition
        if cb is None:
            return
        try:
            await cb(node, transition)
        except Exception:
            pass  # progress notification must never break execution

    def add_step(
        self,
        id: str,
        title: str,
        fn: Callable,
        args: tuple = (),
        kwargs: dict | None = None,
        depends_on: list[str] | None = None,
        can_parallel_with: list[str] | None = None,
    ) -> None:
        """Register a node and the function that executes it."""
        self.graph.add_step(id, title, depends_on=depends_on, can_parallel_with=can_parallel_with)
        self._step_fns[id] = (fn, args, kwargs or {})

    async def execute(self) -> TaskGraph:
        """Execute all nodes respecting dependencies and parallelism limits."""
        semaphore = asyncio.Semaphore(self.max_concurrent)
        groups = self.graph.get_parallel_groups()

        for group in groups:
            tasks = []
            for node in group:
                # Skip nodes whose dependencies failed at runtime (fail-fast)
                if node.status != ExecutionStatus.PENDING:
                    continue
                tasks.append(self._execute_node(node, semaphore))
            await asyncio.gather(*tasks, return_exceptions=True)

        return self.graph

    async def _execute_node(self, node: TaskNode, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            node.status = ExecutionStatus.RUNNING
            node.started_at = time.time()
            await self._fire(node, "started")
            try:
                fn, args, kwargs = self._step_fns[node.id]
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(*args, **kwargs)
                else:
                    result = fn(*args, **kwargs)
                node.status = ExecutionStatus.COMPLETED
                node.result = result
                self._results[node.id] = result
                self.graph._completed.add(node.id)
                await self._fire(node, "completed")
            except Exception as e:
                node.status = ExecutionStatus.FAILED
                node.error = str(e)
                self.graph._failed.add(node.id)
                self._errors[node.id] = str(e)
                self._skip_dependents(node.id)
                await self._fire(node, "failed")
            finally:
                node.completed_at = time.time()

    def _skip_dependents(self, node_id: str) -> None:
        """Recursively mark dependents of a failed node as SKIPPED."""
        for node in self.graph.nodes.values():
            if node.status == ExecutionStatus.PENDING and node_id in node.depends_on:
                node.status = ExecutionStatus.SKIPPED
                node.error = f"Skipped: dependency '{node_id}' failed"
                self._skip_dependents(node.id)
                # A skip is a real transition — fire the callback without
                # blocking the fail-fast path (callback swallows errors).
                asyncio.ensure_future(self._fire(node, "skipped"))

    @property
    def results(self) -> dict[str, Any]:
        return dict(self._results)

    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)
