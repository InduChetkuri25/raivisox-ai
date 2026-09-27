"""RAIVISOX-BENCH — benchmark system for execution intelligence."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
import time


class BenchmarkCategory(str, Enum):
    INTENT = "intent"
    PLANNING = "planning"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    RECOVERY = "recovery"
    END_TO_END = "end_to_end"


class MetricType(str, Enum):
    LATENCY = "latency_ms"
    SUCCESS_RATE = "success_rate"
    QUALITY = "quality_score"
    COST = "cost"
    THROUGHPUT = "throughput"


@dataclass
class BenchmarkCase:
    """A single benchmark case."""

    id: str
    name: str
    category: BenchmarkCategory
    description: str
    setup: dict = field(default_factory=dict)
    expected_outcome: dict = field(default_factory=dict)
    max_allowed_latency: float | None = None  # ms
    min_success_rate: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category.value,
            "description": self.description,
            "setup": self.setup,
            "expected_outcome": self.expected_outcome,
            "max_allowed_latency": self.max_allowed_latency,
            "min_success_rate": self.min_success_rate,
        }


@dataclass
class BenchmarkMetrics:
    """Metrics collected during a benchmark run."""

    case_id: str
    latency_ms: float = 0.0
    success: bool = False
    quality_score: float = 0.0
    cost: float = 0.0
    throughput: float = 0.0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "latency_ms": self.latency_ms,
            "success": self.success,
            "quality_score": self.quality_score,
            "cost": self.cost,
            "throughput": self.throughput,
            "details": self.details,
        }


@dataclass
class BenchmarkResult:
    """Result of a single benchmark case run."""

    case_id: str
    case_name: str
    category: BenchmarkCategory
    passed: bool
    metrics: BenchmarkMetrics
    run_at: float = field(default_factory=time.time)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "case_name": self.case_name,
            "category": self.category.value,
            "passed": self.passed,
            "metrics": self.metrics.to_dict(),
            "run_at": self.run_at,
            "notes": self.notes,
        }


class RAIVISOXBenchRunner:
    """Runs the RAIVISOX-BENCH benchmark suite."""

    DEFAULT_CASES: list[BenchmarkCase] = [
        BenchmarkCase(
            id="bench_intent_parse_01",
            name="Intent Parsing — Simple Request",
            category=BenchmarkCategory.INTENT,
            description="Parse a simple natural language request into structured intent",
            setup={"input": "Create a todo app", "expected_type": "software_build"},
            expected_outcome={"task_type": "software_build"},
            max_allowed_latency=5000,
        ),
        BenchmarkCase(
            id="bench_intent_ambiguity_01",
            name="Intent Parsing — Ambiguity Detection",
            category=BenchmarkCategory.INTENT,
            description="Detect ambiguity in vague requests",
            setup={"input": "Fix the thing", "expected_ambiguous": True},
            expected_outcome={"ambiguity": "high"},
            max_allowed_latency=5000,
        ),
        BenchmarkCase(
            id="bench_plan_strategy_01",
            name="Planning — Strategy Selection",
            category=BenchmarkCategory.PLANNING,
            description="Select the best strategy for a software build task",
            setup={"task_type": "software_build", "risk_level": "medium"},
            expected_outcome={"strategy_selected": True},
            max_allowed_latency=10000,
        ),
        BenchmarkCase(
            id="bench_execution_parallel_01",
            name="Execution — Parallel Step Execution",
            category=BenchmarkCategory.EXECUTION,
            description="Execute independent tasks in parallel",
            setup={"steps": 4, "parallel_groups": 2},
            expected_outcome={"parallel_groups_executed": 2},
            max_allowed_latency=15000,
        ),
        BenchmarkCase(
            id="bench_verification_01",
            name="Verification — Outcome Verification",
            category=BenchmarkCategory.VERIFICATION,
            description="Verify task outcome against criteria",
            setup={"criteria": ["file_exists", "output_matches"]},
            expected_outcome={"verified": True},
            max_allowed_latency=8000,
        ),
        BenchmarkCase(
            id="bench_recovery_01",
            name="Recovery — Failure Recovery",
            category=BenchmarkCategory.RECOVERY,
            description="Recover from a simulated task failure",
            setup={"failure_type": "tool_error", "strategies_available": 3},
            expected_outcome={"recovered": True},
            max_allowed_latency=12000,
        ),
        BenchmarkCase(
            id="bench_end_to_end_01",
            name="End-to-End — Simple Software Build",
            category=BenchmarkCategory.END_TO_END,
            description="Complete end-to-end flow: understand, plan, execute, verify",
            setup={"request": "Build a calculator app", "expected_phases": 4},
            expected_outcome={"all_phases_completed": True},
            max_allowed_latency=30000,
        ),
    ]

    def __init__(self):
        self._cases: list[BenchmarkCase] = list(self.DEFAULT_CASES)
        self._results: list[BenchmarkResult] = []
        self._custom_cases: list[BenchmarkCase] = []

    def add_case(self, case: BenchmarkCase) -> None:
        self._custom_cases.append(case)

    def get_cases(self, category: BenchmarkCategory | None = None) -> list[BenchmarkCase]:
        if category:
            return [c for c in self._cases + self._custom_cases if c.category == category]
        return list(self._cases) + list(self._custom_cases)

    def run_case(self, case: BenchmarkCase, run_fn: Callable) -> BenchmarkResult:
        """Run a single benchmark case with the provided function."""
        start = time.time()
        try:
            result = run_fn(case.setup)
            latency_ms = (time.time() - start) * 1000

            metrics = BenchmarkMetrics(
                case_id=case.id,
                latency_ms=latency_ms,
                success=case.expected_outcome.items() <= result.items() if isinstance(result, dict) else False,
                quality_score=1.0 if (case.expected_outcome.items() <= result.items() if isinstance(result, dict) else False) else 0.0,
                cost=0.0,
                throughput=1.0 / latency_ms * 1000 if latency_ms > 0 else 0.0,
                details={"result": result},
            )

            passed = metrics.success
            if case.max_allowed_latency and latency_ms > case.max_allowed_latency:
                passed = False
                metrics.details["note"] = f"Latency {latency_ms:.0f}ms exceeded max {case.max_allowed_latency}ms"

            return BenchmarkResult(
                case_id=case.id,
                case_name=case.name,
                category=case.category,
                passed=passed,
                metrics=metrics,
                notes="",
            )
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            return BenchmarkResult(
                case_id=case.id,
                case_name=case.name,
                category=case.category,
                passed=False,
                metrics=BenchmarkMetrics(
                    case_id=case.id,
                    latency_ms=latency_ms,
                    success=False,
                    details={"error": str(e)},
                ),
                notes=f"Exception: {e}",
            )

    def run_all(self, run_fn: Callable) -> list[BenchmarkResult]:
        """Run all benchmark cases."""
        self._results = []
        for case in self.get_cases():
            result = self.run_case(case, run_fn)
            self._results.append(result)
        return list(self._results)

    def summary(self) -> dict:
        if not self._results:
            return {"total": 0, "passed": 0, "failed": 0, "pass_rate": 0.0}
        total = len(self._results)
        passed = sum(1 for r in self._results if r.passed)
        return {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": passed / total,
            "by_category": {
                cat.value: {
                    "total": len([r for r in self._results if r.category == cat]),
                    "passed": len([r for r in self._results if r.category == cat and r.passed]),
                }
                for cat in BenchmarkCategory
                if any(r.category == cat for r in self._results)
            },
        }

    def get_results(self, case_id: str | None = None) -> list[BenchmarkResult]:
        if case_id:
            return [r for r in self._results if r.case_id == case_id]
        return list(self._results)

    def clear_results(self) -> None:
        self._results.clear()

