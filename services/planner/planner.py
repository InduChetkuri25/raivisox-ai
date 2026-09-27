"""Multi-Strategy Planner — selects and sequences execution plans.

Evaluates different strategies for accomplishing a task and selects
the best one based on success probability, speed, cost, reliability, risk,
and available capabilities.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class StrategyApproach(str, Enum):
    """High-level approaches to task execution."""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    BATCHED = "batched"
    PREPARED = "prepared"
    ADAPTIVE = "adaptive"


class StrategySpeed(str, Enum):
    FAST = "fast"
    MEDIUM = "medium"
    SLOW = "slow"


class StrategyReliability(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class StrategyCost(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class Strategy:
    """A possible execution strategy for a task."""

    id: str
    name: str
    description: str
    approach: StrategyApproach
    estimated_speed: StrategySpeed
    estimated_reliability: StrategyReliability
    estimated_cost: StrategyCost
    required_capabilities: list[str] = field(default_factory=list)
    confidence: float = 0.5
    rationale: str = ""

    def score(self) -> float:
        """Compute a composite score for this strategy.

        Higher is better. Factors in speed, reliability, cost, and confidence.
        """
        speed_map = {StrategySpeed.FAST: 3, StrategySpeed.MEDIUM: 2, StrategySpeed.SLOW: 1}
        reliability_map = {StrategyReliability.HIGH: 3, StrategyReliability.MEDIUM: 2, StrategyReliability.LOW: 1}
        cost_map = {StrategyCost.LOW: 3, StrategyCost.MEDIUM: 2, StrategyCost.HIGH: 1}

        return (
            speed_map[self.estimated_speed] * 0.25 +
            reliability_map[self.estimated_reliability] * 0.35 +
            cost_map[self.estimated_cost] * 0.2 +
            self.confidence * 0.2
        )


@dataclass
class PlanStep:
    """A single step in the execution plan."""

    id: str
    order: int
    title: str
    description: str
    expected_output: str = ""
    capability_required: str = ""
    can_parallel_with: list[str] = field(default_factory=list)
    status: str = "pending"
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ExecutionPlan:
    """A complete execution plan for a task."""

    task_id: str
    strategy: Strategy
    steps: list[PlanStep] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def parallelizable_steps(self) -> int:
        return sum(1 for s in self.steps if s.can_parallel_with)


class Planner:
    """Generates execution plans using strategy evaluation.

    For each task, generates multiple candidate strategies,
    scores them, and produces an execution plan using the best one.
    """

    def __init__(self):
        self._step_counter = 0

    def plan(self, task_id: str, goal: str, task_type: str,
             required_capabilities: list[str],
             risk_level: str = "low",
             ambiguity: str = "clear",
             ) -> ExecutionPlan:
        """Generate an execution plan for a task.

        Args:
            task_id: The task identifier.
            goal: The task goal.
            task_type: The type of task.
            required_capabilities: Capabilities needed to execute.
            risk_level: The risk level of the task.
            ambiguity: How ambiguous the request is.

        Returns:
            An ExecutionPlan with the selected strategy and steps.
        """
        self._step_counter = 0

        # Generate candidate strategies
        strategies = self._generate_strategies(
            task_id, goal, task_type, required_capabilities, risk_level, ambiguity
        )

        # Select the best strategy
        selected = self._select_best(strategies)

        # Generate the plan steps using the selected strategy
        steps = self._generate_steps(selected, task_type, required_capabilities)

        return ExecutionPlan(
            task_id=task_id,
            strategy=selected,
            steps=steps,
        )

    def _generate_strategies(
        self, task_id: str, goal: str, task_type: str,
        required_capabilities: list[str], risk_level: str, ambiguity: str,
    ) -> list[Strategy]:
        """Generate candidate strategies for the task."""
        strategies: list[Strategy] = []
        counter = [0]

        def add_strategy(
            name: str,
            description: str,
            approach: StrategyApproach,
            speed: StrategySpeed,
            reliability: StrategyReliability,
            cost: StrategyCost,
            capabilities: Optional[list[str]] = None,
            confidence: float = 0.5,
            rationale: str = "",
        ) -> None:
            counter[0] += 1
            strategies.append(Strategy(
                id=f"strategy-{counter[0]}",
                name=name,
                description=description,
                approach=approach,
                estimated_speed=speed,
                estimated_reliability=reliability,
                estimated_cost=cost,
                required_capabilities=capabilities or [],
                confidence=confidence,
                rationale=rationale,
            ))

        # Always generate a baseline sequential strategy
        add_strategy(
            name="Standard Sequential",
            description="Execute steps one after another in a straightforward manner.",
            approach=StrategyApproach.SEQUENTIAL,
            speed=StrategySpeed.MEDIUM,
            reliability=StrategyReliability.MEDIUM,
            cost=StrategyCost.LOW,
            rationale="Safe default that works for most tasks.",
        )

        # Generate task-type-specific strategies
        if task_type == "software_build":
            add_strategy(
                name="Parallel Development",
                description="Build core logic, UI, and data layer in parallel where possible.",
                approach=StrategyApproach.PARALLEL,
                speed=StrategySpeed.FAST,
                reliability=StrategyReliability.MEDIUM,
                cost=StrategyCost.MEDIUM,
                capabilities=["code_generation", "filesystem_write"],
                rationale="Reduces total execution time by running independent phases concurrently.",
                confidence=0.7,
            )
            if risk_level == "high":
                add_strategy(
                    name="Prepared Execution",
                    description="Plan thoroughly, prepare environment, then execute with checkpoints.",
                    approach=StrategyApproach.PREPARED,
                    speed=StrategySpeed.SLOW,
                    reliability=StrategyReliability.HIGH,
                    cost=StrategyCost.MEDIUM,
                    rationale="Higher reliability for high-risk production builds, at the cost of speed.",
                    confidence=0.6,
                )

        elif task_type == "debugging":
            add_strategy(
                name="Diagnose-Then-Fix",
                description="First diagnose the root cause thoroughly, then apply the fix.",
                approach=StrategyApproach.SEQUENTIAL,
                speed=StrategySpeed.MEDIUM,
                reliability=StrategyReliability.HIGH,
                cost=StrategyCost.LOW,
                rationale="Methodical debugging reduces the chance of misdiagnosis.",
                confidence=0.8,
            )

        elif task_type == "research":
            add_strategy(
                name="Parallel Search",
                description="Search multiple sources in parallel, then synthesize.",
                approach=StrategyApproach.PARALLEL,
                speed=StrategySpeed.FAST,
                reliability=StrategyReliability.MEDIUM,
                cost=StrategyCost.MEDIUM,
                rationale="Parallel research gathers broader coverage faster.",
                confidence=0.65,
            )

        elif task_type == "file_operation":
            add_strategy(
                name="Direct Execution",
                description="Execute the file operation directly with verification.",
                approach=StrategyApproach.SEQUENTIAL,
                speed=StrategySpeed.FAST,
                reliability=StrategyReliability.HIGH,
                cost=StrategyCost.LOW,
                rationale="Simple file operations are best done directly and verified.",
                confidence=0.9,
            )

        # If ambiguity is high, add a clarification-first strategy
        if ambiguity == "high" or ambiguity == "critical":
            add_strategy(
                name="Clarify-First",
                description="Pause to request clarification before executing.",
                approach=StrategyApproach.PREPARED,
                speed=StrategySpeed.SLOW,
                reliability=StrategyReliability.HIGH,
                cost=StrategyCost.LOW,
                rationale="Resolving ambiguity upfront prevents wasted execution.",
                confidence=0.85,
            )

        return strategies

    def _select_best(self, strategies: list[Strategy]) -> Strategy:
        """Select the best strategy from candidates."""
        if not strategies:
            raise ValueError("No strategies generated")

        # Score and pick the highest
        scored = [(s.score(), s) for s in strategies]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    def _generate_steps(
        self, strategy: Strategy, task_type: str, required_capabilities: list[str],
    ) -> list[PlanStep]:
        """Generate plan steps based on the selected strategy."""
        steps: list[PlanStep] = []
        counter = [0]

        def next_step(
            title: str,
            description: str,
            capability: str = "",
            parallel_with: Optional[list[str]] = None,
            expected_output: str = "",
        ) -> PlanStep:
            counter[0] += 1
            return PlanStep(
                id=f"plan-step-{counter[0]}",
                order=counter[0],
                title=title,
                description=description,
                capability_required=capability,
                can_parallel_with=parallel_with or [],
                expected_output=expected_output,
            )

        # Generate strategy-specific step sequences
        if strategy.approach == StrategyApproach.SEQUENTIAL:
            steps = self._sequential_steps(task_type, required_capabilities, next_step)
        elif strategy.approach == StrategyApproach.PARALLEL:
            steps = self._parallel_steps(task_type, required_capabilities, next_step)
        elif strategy.approach == StrategyApproach.PREPARED:
            steps = self._prepared_steps(task_type, required_capabilities, next_step)
        else:
            steps = self._sequential_steps(task_type, required_capabilities, next_step)

        return steps

    def _sequential_steps(
        self, task_type: str, capabilities: list[str], next_step,
    ) -> list[PlanStep]:
        """Generate sequential plan steps."""
        steps = []

        steps.append(next_step(
            title="Understand the Request",
            description="Parse and analyze the user's request to extract goals and requirements.",
            capability="intent_parsing",
            expected_output="Structured understanding of the task",
        ))
        steps.append(next_step(
            title="Plan the Approach",
            description="Determine the execution strategy and sequence of actions.",
            capability="planning",
            expected_output="Execution plan with ordered steps",
        ))

        if task_type == "software_build":
            steps.append(next_step(
                title="Design Architecture",
                description="Design the application architecture and technology choices.",
                capability="architecture_design",
                expected_output="Architecture design document",
            ))
            steps.append(next_step(
                title="Set Up Project",
                description="Create project structure, config files, and dependencies.",
                capability="filesystem_write",
                expected_output="Initialized project structure",
            ))
            steps.append(next_step(
                title="Implement Code",
                description="Write the application code.",
                capability="code_generation",
                expected_output="Complete application source code",
            ))
            steps.append(next_step(
                title="Build and Test",
                description="Build the application and run tests.",
                capability="terminal_execute",
                expected_output="Successful build and passing tests",
            ))
            steps.append(next_step(
                title="Verify and Deliver",
                description="Verify the result and deliver to the user.",
                capability="verification",
                expected_output="Verified result with documentation",
            ))

        elif task_type == "debugging":
            steps.append(next_step(
                title="Reproduce the Issue",
                description="Set up the environment and reproduce the error.",
                capability="terminal_execute",
                expected_output="Confirmed reproduction of the issue",
            ))
            steps.append(next_step(
                title="Diagnose Root Cause",
                description="Analyze the code and behavior to find the root cause.",
                capability="error_analysis",
                expected_output="Identified root cause",
            ))
            steps.append(next_step(
                title="Apply Fix",
                description="Implement the fix for the root cause.",
                capability="code_generation",
                expected_output="Fixed code",
            ))
            steps.append(next_step(
                title="Verify Fix",
                description="Re-run the scenario to confirm the issue is resolved.",
                capability="test_execution",
                expected_output="Verified fix with no regressions",
            ))

        elif task_type == "research":
            steps.append(next_step(
                title="Search Sources",
                description="Search for relevant information from appropriate sources.",
                capability="web_search",
                expected_output="Gathered research materials",
            ))
            steps.append(next_step(
                title="Synthesize Findings",
                description="Organize and summarize the gathered information.",
                capability="document_generation",
                expected_output="Coherent research summary",
            ))

        elif task_type == "file_operation":
            steps.append(next_step(
                title="Locate Target",
                description="Find the files or directories involved.",
                capability="filesystem_read",
                expected_output="Located target files",
            ))
            steps.append(next_step(
                title="Perform Operation",
                description="Execute the requested file operation.",
                capability="filesystem_write",
                expected_output="Completed file operation",
            ))
            steps.append(next_step(
                title="Verify Result",
                description="Confirm the operation completed as expected.",
                capability="filesystem_read",
                expected_output="Verified result",
            ))

        else:
            steps.append(next_step(
                title="Execute Task",
                description="Perform the work to fulfill the request.",
                capability="",
                expected_output="Completed task",
            ))
            steps.append(next_step(
                title="Verify and Deliver",
                description="Verify the result and deliver to the user.",
                capability="verification",
                expected_output="Verified result",
            ))

        return steps

    def _parallel_steps(
        self, task_type: str, capabilities: list[str], next_step,
    ) -> list[PlanStep]:
        """Generate parallel-capable plan steps."""
        steps = []

        # Always start with understanding
        steps.append(next_step(
            title="Understand the Request",
            description="Parse and analyze the user's request.",
            capability="intent_parsing",
            expected_output="Structured understanding",
        ))

        if task_type == "software_build":
            steps.append(next_step(
                title="Design Architecture",
                description="Design the application architecture.",
                capability="architecture_design",
                expected_output="Architecture design",
            ))

            # These can run in parallel
            steps.append(next_step(
                title="Implement Core Logic",
                description="Write the core business logic and models.",
                capability="code_generation",
                parallel_with=["Implement UI", "Set Up Data Layer"],
                expected_output="Core application logic",
            ))
            steps.append(next_step(
                title="Implement UI",
                description="Build the user interface components.",
                capability="code_generation",
                parallel_with=["Implement Core Logic", "Set Up Data Layer"],
                expected_output="User interface components",
            ))
            steps.append(next_step(
                title="Set Up Data Layer",
                description="Create database schemas and data access layer.",
                capability="code_generation",
                parallel_with=["Implement Core Logic", "Implement UI"],
                expected_output="Data layer implementation",
            ))

            steps.append(next_step(
                title="Integrate and Build",
                description="Integrate all components and build the application.",
                capability="terminal_execute",
                expected_output="Successful build",
            ))
            steps.append(next_step(
                title="Verify and Deliver",
                description="Verify the result and deliver to the user.",
                capability="verification",
                expected_output="Verified result",
            ))

        else:
            # Fallback to sequential for other types
            steps.extend(self._sequential_steps(task_type, capabilities, next_step))

        return steps

    def _prepared_steps(
        self, task_type: str, capabilities: list[str], next_step,
    ) -> list[PlanStep]:
        """Generate prepared execution steps with checkpoints."""
        steps = self._sequential_steps(task_type, capabilities, next_step)

        # Add checkpoint steps
        for i, step in enumerate(steps):
            if step.order > 1 and step.order % 2 == 0:
                # Insert a checkpoint after every other step
                checkpoint = PlanStep(
                    id=f"checkpoint-{step.order}",
                    order=step.order + 0.5,  # Won't conflict
                    title=f"Checkpoint: Review {step.title}",
                    description=f"Review the output of '{step.title}' before proceeding.",
                    capability="verification",
                    expected_output="Checkpoint review passed",
                )
                steps.append(checkpoint)

        # Sort by order
        steps.sort(key=lambda s: s.order)
        # Re-assign order
        for i, step in enumerate(steps):
            step.order = i + 1

        return steps


_engine: Optional[Planner] = None


def get_planner() -> Planner:
    """Get the shared Planner instance."""
    global _engine
    if _engine is None:
        _engine = Planner()
    return _engine
