"""Resource quotas and execution budgets — V2 Phase 5 (design §5/§7.6).

One consistent representation of resource limits for every backend:

- ``SandboxQuota``: the explicit, reviewable resource grant. Backends apply
  whatever their platform can honestly enforce and REFUSE spec fields they
  cannot (honoring ``strict_limits``), so a caller never silently runs with
  weaker limits than it asked for.
- ``QuotaTermination``: the vocabulary of quota terminations (design §5):
  timeout, memory, cpu, pids, fsize — plus ``supervisor`` for
  backend/cleanup failures. Ordinary non-zero process exits are NOT quota
  terminations and never map onto this vocabulary.
- ``ExecutionBudget``: a cumulative, **per-adapted-execution-context**
  budget (design §7.6 "per-task cumulative budget"). It is carried by the
  step-scoped object that already reaches into every sandbox call — not by
  a global — because RAIVISOX's pipeline serializes steps per actor, so a
  global counter could undercount nothing: contexts are the unit of work.
  Every sandbox run reserves units first (hard-block when exhausted — a
  budget is a limit, not an advisory), consumes actual units after, and
  reports honestly.
- ``QuotaEnforcer``: app-layer walls that need no OS support: bounded
  artifact/file writes (T8, disk exhaustion) and a wall-clock budget
  expiry check.

Honesty rules (unchanged from Phase 1): limits are claimed as enforced
only when the platform actually applied them; unsupported limits are
reported as unsupported, never converted into passes; when attribution of
a child death is uncertain the backend reports a generic supervisor
failure instead of inventing a cause.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "QuotaTermination",
    "SandboxQuota",
    "BudgetExceeded",
    "ExecutionBudget",
    "QuotaEnforcer",
]

QuotaTermination = Literal["timeout", "memory", "cpu", "pids", "fsize",
                           "supervisor"]

# Canonical (lowercased) names of the sandbox limits a caller may require.
_SANDBOX_LIMIT_NAMES = (
    "cpu_limit", "memory_limit", "fsize_limit", "pids_limit",
    "network_deny", "fs_confinement", "timeout_kill",
)


@dataclass(frozen=True)
class SandboxQuota:
    """The resource grant for an execution, explicit and reviewable.

    ``required_limits`` lists (case-insensitive) capability names the CALLER
    insists on: if a backend cannot enforce one of them it must refuse the
    run honestly (honoring ``strict_limits=False`` downgrades this to a
    warning surfaced via the result's ``limits_hit``). Default is strict
    about the core guarantees every backend already provides; CPU/memory/
    fsize are opt-in requirements because the process backend can only
    enforce them on POSIX (and honestly reports them unsupported
    elsewhere), while the container backend maps them to real runtime
    controls.

    Wall-clock timeout lives in ``SandboxSpec.timeout_s`` (per execution);
    cumulative wall-clock belongs to :class:`ExecutionBudget`.
    """

    cpu_seconds: int | None = None
    memory_bytes: int | None = None
    max_pids: int | None = None
    fsize_bytes: int | None = None
    output_limit_bytes: int = 1_048_576
    required_limits: tuple[str, ...] = (
        "argv_execution", "minimal_env", "working_dir", "bounded_output",
        "timeout_kill",
    )
    strict_limits: bool = True

    def require(self, *limit_names: str) -> "SandboxQuota":
        """A quota demanding additional enforced capabilities."""
        extra = tuple(n.lower() for n in limit_names
                      if n.lower() not in self.required_limits)
        return SandboxQuota(
            cpu_seconds=self.cpu_seconds,
            memory_bytes=self.memory_bytes,
            max_pids=self.max_pids,
            fsize_bytes=self.fsize_bytes,
            output_limit_bytes=self.output_limit_bytes,
            required_limits=self.required_limits + extra,
            strict_limits=self.strict_limits,
        )

    def to_spec_kwargs(self) -> dict:
        """Map onto the existing SandboxSpec fields (no duplicate system)."""
        kwargs: dict = {"output_limit_bytes": self.output_limit_bytes}
        if self.cpu_seconds is not None:
            kwargs["cpu_seconds"] = self.cpu_seconds
        if self.memory_bytes is not None:
            kwargs["memory_bytes"] = self.memory_bytes
        if self.max_pids is not None:
            kwargs["max_pids"] = self.max_pids
        if self.fsize_bytes is not None:
            kwargs["fsize_bytes"] = self.fsize_bytes
        return kwargs

    def missing_from(self, capabilities: dict[str, bool]) -> list[str]:
        """Required limits this host/backend cannot enforce (spec-level)."""
        return [name for name in self.required_limits
                if not capabilities.get(name, False)]


@dataclass
class BudgetExceeded(Exception):
    """Raised (or returned) when a cumulative budget is exhausted."""

    scope: str
    units_used: float
    units_limit: float
    kind: str  # "cpu" | "wall_clock" | "disk"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return (f"execution budget exceeded in {self.scope}: "
                f"{self.units_used:.3f}/{self.units_limit:g} {self.kind}")


@dataclass
class ExecutionBudget:
    """Cumulative resource budget for one adapted execution context.

    Limits are cumulative across every sandbox run performed under the
    budget (retries and recovery included — the design's T6/T7 concern
    about a task's aggregate usage being unbounded). ``None`` disables a
    dimension. ``cpu_seconds`` counts actual measured CPU time (real
    process CPU where the platform exposes it, wall-clock otherwise —
    the stored basis is recorded in ``cpu_basis``).
    """

    cpu_seconds: float | None = None
    wall_clock_seconds: float | None = None
    max_runs: int | None = None

    scope: str = "execution"
    _cpu_used: float = 0.0
    _wall_used: float = 0.0
    _runs: int = 0
    _cpu_basis: str = "wall_clock"
    _started_at: float = field(default_factory=time.monotonic)
    _wall_expired: bool = False

    @property
    def cpu_basis(self) -> str:
        return self._cpu_basis

    @property
    def cpu_used(self) -> float:
        return self._cpu_used

    @property
    def wall_used(self) -> float:
        return self._wall_used

    @property
    def runs(self) -> int:
        return self._runs

    def _now(self) -> float:
        return time.monotonic()

    def check_reserve(self, *, kind: str = "run", units: float = 0.0) -> None:
        """Hard gate before a run: raise when a dimension is exhausted.

        ``kind`` on the raised ``BudgetExceeded`` names the EXHAUSTED
        dimension (``wall_clock`` | ``cpu`` | ``runs``) so the sandbox maps
        it onto the honest quota vocabulary — run-count exhaustion is a
        supervisor-class policy stop, never a fake "cpu" kill.
        """
        if self.max_runs is not None and self._runs >= self.max_runs:
            raise BudgetExceeded(self.scope, float(self._runs),
                                 float(self.max_runs), "runs")
        if self.wall_clock_seconds is not None:
            self._advance_wall()
            if self._wall_expired:
                raise BudgetExceeded(self.scope, self._wall_used,
                                     self.wall_clock_seconds, "wall_clock")
        if self.cpu_seconds is not None and self._cpu_used >= self.cpu_seconds:
            raise BudgetExceeded(self.scope, self._cpu_used, self.cpu_seconds,
                                 "cpu")
        del kind, units  # units reserved per-run are the run's own timeout

    def _advance_wall(self) -> None:
        if self._wall_expired:
            return
        elapsed = self._now() - self._started_at
        if self.wall_clock_seconds is not None and elapsed >= self.wall_clock_seconds:
            self._wall_expired = True
            self._wall_used = max(self._wall_used, elapsed)

    def record_usage(self, *, cpu_seconds: float | None = None,
                     wall_seconds: float | None = None,
                     cpu_basis: str = "wall_clock",
                     runs: int = 0) -> None:
        """Consume actual units after a run completes (honest accounting)."""
        if cpu_seconds is not None:
            self._cpu_used += max(0.0, cpu_seconds)
            # Upgrading the recorded basis to a real CPU-time measurement is
            # monotone-honest; it never downgrades an actual measurement.
            if cpu_basis == "cpu_time":
                self._cpu_basis = "cpu_time"
        if wall_seconds is not None:
            self._wall_used += max(0.0, wall_seconds)
        self._runs += runs

    def wall_clock_expired(self) -> bool:
        self._advance_wall()
        return self._wall_expired

    def status(self) -> dict:
        """Honest budget state for results/health surfaces."""
        self._advance_wall()
        return {
            "scope": self.scope,
            "cpu_seconds_limit": self.cpu_seconds,
            "cpu_used": round(self._cpu_used, 3),
            "cpu_basis": self._cpu_basis,
            "wall_clock_seconds_limit": self.wall_clock_seconds,
            "wall_clock_used": round(self._wall_used, 3),
            "max_runs": self.max_runs,
            "runs": self._runs,
            "exhausted": self._exhausted_dimensions(),
        }

    def _exhausted_dimensions(self) -> list[str]:
        out: list[str] = []
        if self.max_runs is not None and self._runs >= self.max_runs:
            out.append("max_runs")
        if self._wall_expired:
            out.append("wall_clock")
        if self.cpu_seconds is not None and self._cpu_used >= self.cpu_seconds:
            out.append("cpu")
        return out


class QuotaEnforcer:
    """App-layer quota walls that need no OS support.

    - :meth:`check_write` bounds file writes into the workspace (T8 disk
      exhaustion) at the point every workspace write goes through; it
      never reads or follows the target's content — only sizes.
    - :meth:`check_budget_expiry` converts a wall-clock budget expiry into
      the honest ``timeout`` quota vocabulary.
    """

    def __init__(self, budget: ExecutionBudget | None = None,
                 max_write_bytes: int | None = None) -> None:
        self._budget = budget
        self._max_write_bytes = max_write_bytes
        self._writes_rejected = 0

    @property
    def writes_rejected(self) -> int:
        return self._writes_rejected

    def check_write(self, path: str, data: bytes | bytearray | str) -> None:
        """Refuse oversized workspace writes BEFORE any bytes land.

        Raises ``BudgetExceeded(kind="disk")`` — disk exhaustion is its own
        dimension, reported through the caller's failure channel as a
        supervisor-class resource failure (never a fake process kill and
        never an ordinary exit code).
        """
        size = len(data.encode() if isinstance(data, str) else bytes(data))
        if self._max_write_bytes is not None and size > self._max_write_bytes:
            self._writes_rejected += 1
            raise BudgetExceeded(scope=f"write:{os.path.basename(path)}",
                                 units_used=float(size),
                                 units_limit=float(self._max_write_bytes),
                                 kind="disk")
        del path  # name used only for the error scope

    def check_budget_expiry(self) -> None:
        """Wall-clock budget expiry → honest ``timeout`` semantics."""
        if self._budget is not None and self._budget.wall_clock_expired():
            raise BudgetExceeded(self._budget.scope,
                                 self._budget.wall_used,
                                 self._budget.wall_clock_seconds or 0.0,
                                 "wall_clock")

    def record_run(self, *, cpu_seconds: float | None,
                   wall_seconds: float | None,
                   cpu_basis: str = "wall_clock") -> None:
        if self._budget is not None:
            self._budget.record_usage(cpu_seconds=cpu_seconds,
                                      wall_seconds=wall_seconds,
                                      cpu_basis=cpu_basis, runs=1)
