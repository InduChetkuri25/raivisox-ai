"""Task Decomposer — breaks an Intent into actionable steps.

Takes a structured Intent and produces a list of discrete,
verifiable steps that can be executed by the system.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from services.intent.engine import (
    DefinitionOfDone,
    Intent,
    Requirement,
    TaskType,
)


@dataclass
class TaskStep:
    """A single decomposable step derived from an intent."""

    id: str
    title: str
    description: str
    step_type: str  # understand, plan, prepare, execute, verify, deliver
    order: int
    dependencies: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    estimated_effort: str = "medium"  # trivial, small, medium, large
    can_parallelize: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)


class TaskDecomposer:
    """Breaks an Intent into a sequence of TaskSteps.

    The decomposer uses the task type and requirements from the
    Intent to generate an appropriate decomposition. In production,
    this would be model-assisted. For now it uses rule-based
    decomposition that is clean and replaceable.
    """

    def __init__(self):
        self._step_counter = 0

    def decompose(self, intent: Intent) -> list[TaskStep]:
        """Decompose an Intent into actionable steps.

        Args:
            intent: The structured Intent to decompose.

        Returns:
            A list of TaskStep objects in execution order.
        """
        self._step_counter = 0
        steps: list[TaskStep] = []

        if intent.task_type == TaskType.SOFTWARE_BUILD:
            steps = self._decompose_software_build(intent)
        elif intent.task_type == TaskType.DEBUGGING:
            steps = self._decompose_debugging(intent)
        elif intent.task_type == TaskType.RESEARCH:
            steps = self._decompose_research(intent)
        elif intent.task_type == TaskType.FILE_OPERATION:
            steps = self._decompose_file_operation(intent)
        elif intent.task_type == TaskType.DATA_ANALYSIS:
            steps = self._decompose_data_analysis(intent)
        else:
            steps = self._decompose_generic(intent)

        # Link dependencies
        self._resolve_dependencies(steps)

        return steps

    def _resolve_dependencies(self, steps: list[TaskStep]) -> None:
        """Resolve step dependencies based on order and type."""
        for i, step in enumerate(steps):
            # Each step depends on the previous one unless marked parallelizable
            if i > 0 and not step.can_parallelize:
                steps[i].dependencies = [steps[i - 1].id]

    # --- Decomposer methods per task type ---

    def _decompose_software_build(self, intent: Intent) -> list[TaskStep]:
        """Decompose a software build request."""
        steps = []
        counter = [0]

        def next_step(
            title: str,
            description: str,
            step_type: str = "execute",
            effort: str = "medium",
            capabilities: Optional[list[str]] = None,
            parallel: bool = False,
        ) -> TaskStep:
            counter[0] += 1
            return TaskStep(
                id=f"step-{counter[0]}",
                title=title,
                description=description,
                step_type=step_type,
                order=counter[0],
                required_capabilities=capabilities or [],
                estimated_effort=effort,
                can_parallelize=parallel,
            )

        # Phase 1: Understand & Plan
        steps.append(next_step(
            title="Understand the Request",
            description="Analyze the user's request to extract goals, requirements, constraints, and definition of done.",
            step_type="understand",
            effort="trivial",
            capabilities=["intent_parsing"],
        ))
        steps.append(next_step(
            title="Define Success Criteria",
            description="Create a clear definition of done based on the requirements and constraints.",
            step_type="plan",
            effort="small",
            capabilities=["planning"],
        ))
        steps.append(next_step(
            title="Design Architecture",
            description="Determine the application architecture, technology choices, and project structure.",
            step_type="plan",
            effort="medium",
            capabilities=["architecture_design"],
        ))

        # Phase 2: Prepare
        steps.append(next_step(
            title="Set Up Project Structure",
            description="Create the project directory structure, initialize version control, and set up configuration files.",
            step_type="prepare",
            effort="small",
            capabilities=["filesystem_write", "terminal_execute"],
        ))

        # Phase 3: Build (core)
        steps.append(next_step(
            title="Implement Core Application",
            description="Write the main application code including models, business logic, and core features.",
            step_type="execute",
            effort="large",
            capabilities=["code_generation", "filesystem_write"],
        ))
        steps.append(next_step(
            title="Implement User Interface",
            description="Build the UI components and connect them to the application logic.",
            step_type="execute",
            effort="large",
            capabilities=["code_generation", "filesystem_write"],
            parallel=True,
        ))
        steps.append(next_step(
            title="Set Up Database / Data Layer",
            description="Create database schemas, migrations, and data access layer if the application requires persistence.",
            step_type="execute",
            effort="medium",
            capabilities=["code_generation", "filesystem_write"],
            parallel=True,
        ))

        # Phase 4: Verify
        steps.append(next_step(
            title="Build the Application",
            description="Compile/build the application and fix any build errors.",
            step_type="verify",
            effort="medium",
            capabilities=["terminal_execute", "code_generation"],
        ))
        steps.append(next_step(
            title="Run the Application",
            description="Start the application and verify it runs without errors.",
            step_type="verify",
            effort="small",
            capabilities=["terminal_execute"],
        ))
        steps.append(next_step(
            title="Test Core Functionality",
            description="Verify the application works as intended by testing the primary user flows.",
            step_type="verify",
            effort="medium",
            capabilities=["test_execution", "terminal_execute"],
        ))

        # Phase 5: Deliver
        steps.append(next_step(
            title="Deliver the Result",
            description="Package the application, provide run instructions, and summarize what was built.",
            step_type="deliver",
            effort="small",
            capabilities=["filesystem_write"],
        ))

        return steps

    def _decompose_debugging(self, intent: Intent) -> list[TaskStep]:
        """Decompose a debugging request."""
        steps = []
        counter = [0]

        def next_step(
            title: str,
            description: str,
            step_type: str = "execute",
            effort: str = "medium",
            capabilities: Optional[list[str]] = None,
        ) -> TaskStep:
            counter[0] += 1
            return TaskStep(
                id=f"step-{counter[0]}",
                title=title,
                description=description,
                step_type=step_type,
                order=counter[0],
                required_capabilities=capabilities or [],
                estimated_effort=effort,
            )

        steps.append(next_step(
            title="Understand the Problem",
            description="Analyze the error, symptom, or issue description to understand what is failing and expected behavior.",
            step_type="understand",
            effort="small",
            capabilities=["error_analysis"],
        ))
        steps.append(next_step(
            title="Locate the Code",
            description="Find the relevant code files, functions, or configurations related to the issue.",
            step_type="execute",
            effort="small",
            capabilities=["code_read", "filesystem_read"],
        ))
        steps.append(next_step(
            title="Reproduce the Issue",
            description="Set up the environment and reproduce the error to confirm the problem.",
            step_type="execute",
            effort="medium",
            capabilities=["terminal_execute", "test_execution"],
        ))
        steps.append(next_step(
            title="Diagnose Root Cause",
            description="Analyze the code and behavior to identify the root cause of the issue.",
            step_type="execute",
            effort="medium",
            capabilities=["error_analysis", "code_read"],
        ))
        steps.append(next_step(
            title="Implement Fix",
            description="Apply the fix to the code to resolve the root cause.",
            step_type="execute",
            effort="medium",
            capabilities=["code_generation", "filesystem_write"],
        ))
        steps.append(next_step(
            title="Verify the Fix",
            description="Re-run the failing scenario to confirm the issue is resolved.",
            step_type="verify",
            effort="small",
            capabilities=["test_execution", "terminal_execute"],
        ))
        steps.append(next_step(
            title="Check for Regressions",
            description="Run existing tests to ensure the fix didn't break anything else.",
            step_type="verify",
            effort="medium",
            capabilities=["test_execution"],
        ))
        steps.append(next_step(
            title="Deliver the Result",
            description="Summarize what was diagnosed, what was fixed, and provide the updated code.",
            step_type="deliver",
            effort="small",
            capabilities=["filesystem_write"],
        ))

        return steps

    def _decompose_research(self, intent: Intent) -> list[TaskStep]:
        """Decompose a research request."""
        steps = []
        counter = [0]

        def next_step(
            title: str,
            description: str,
            step_type: str = "execute",
            effort: str = "medium",
            capabilities: Optional[list[str]] = None,
        ) -> TaskStep:
            counter[0] += 1
            return TaskStep(
                id=f"step-{counter[0]}",
                title=title,
                description=description,
                step_type=step_type,
                order=counter[0],
                required_capabilities=capabilities or [],
                estimated_effort=effort,
            )

        steps.append(next_step(
            title="Understand the Research Question",
            description="Clarify what information is being sought and the scope of the research.",
            step_type="understand",
            effort="small",
            capabilities=["intent_parsing"],
        ))
        steps.append(next_step(
            title="Plan Research Strategy",
            description="Determine which sources to consult, what search queries to use, and how to organize findings.",
            step_type="plan",
            effort="small",
            capabilities=["planning"],
        ))
        steps.append(next_step(
            title="Gather Information",
            description="Search and read relevant sources to collect the needed information.",
            step_type="execute",
            effort="medium",
            capabilities=["web_search", "web_read"],
        ))
        steps.append(next_step(
            title="Synthesize Findings",
            description="Organize and summarize the gathered information into a coherent answer.",
            step_type="execute",
            effort="medium",
            capabilities=["document_generation"],
        ))
        steps.append(next_step(
            title="Verify Information",
            description="Cross-check key facts across multiple sources for accuracy.",
            step_type="verify",
            effort="small",
            capabilities=["web_read"],
        ))
        steps.append(next_step(
            title="Deliver the Result",
            description="Present the research findings with sources and any relevant caveats.",
            step_type="deliver",
            effort="small",
        ))

        return steps

    def _decompose_file_operation(self, intent: Intent) -> list[TaskStep]:
        """Decompose a file operation request."""
        steps = []
        counter = [0]

        def next_step(
            title: str,
            description: str,
            step_type: str = "execute",
            effort: str = "medium",
            capabilities: Optional[list[str]] = None,
        ) -> TaskStep:
            counter[0] += 1
            return TaskStep(
                id=f"step-{counter[0]}",
                title=title,
                description=description,
                step_type=step_type,
                order=counter[0],
                required_capabilities=capabilities or [],
                estimated_effort=effort,
            )

        steps.append(next_step(
            title="Understand the Request",
            description="Parse the file operation request to determine the exact operation, target files, and expected result.",
            step_type="understand",
            effort="small",
            capabilities=["intent_parsing"],
        ))
        steps.append(next_step(
            title="Locate Target Files",
            description="Find the files or directories involved in the operation.",
            step_type="execute",
            effort="small",
            capabilities=["filesystem_read"],
        ))
        steps.append(next_step(
            title="Perform the Operation",
            description="Execute the file operation (read, write, edit, move, copy, delete, etc.).",
            step_type="execute",
            effort="medium",
            capabilities=["filesystem_write", "filesystem_read"],
        ))
        steps.append(next_step(
            title="Verify the Result",
            description="Confirm the file operation completed successfully and produced the expected result.",
            step_type="verify",
            effort="small",
            capabilities=["filesystem_read"],
        ))
        steps.append(next_step(
            title="Deliver the Result",
            description="Report the outcome of the file operation.",
            step_type="deliver",
            effort="trivial",
        ))

        return steps

    def _decompose_data_analysis(self, intent: Intent) -> list[TaskStep]:
        """Decompose a data analysis request."""
        steps = []
        counter = [0]

        def next_step(
            title: str,
            description: str,
            step_type: str = "execute",
            effort: str = "medium",
            capabilities: Optional[list[str]] = None,
        ) -> TaskStep:
            counter[0] += 1
            return TaskStep(
                id=f"step-{counter[0]}",
                title=title,
                description=description,
                step_type=step_type,
                order=counter[0],
                required_capabilities=capabilities or [],
                estimated_effort=effort,
            )

        steps.append(next_step(
            title="Understand the Analysis Goal",
            description="Clarify what insights or answers are being sought from the data.",
            step_type="understand",
            effort="small",
            capabilities=["intent_parsing"],
        ))
        steps.append(next_step(
            title="Locate and Load Data",
            description="Find the data source and load it into an analysis environment.",
            step_type="execute",
            effort="small",
            capabilities=["filesystem_read", "data_parse"],
        ))
        steps.append(next_step(
            title="Explore and Clean Data",
            description="Inspect the data for quality issues, missing values, and formatting problems.",
            step_type="execute",
            effort="medium",
            capabilities=["data_parse", "code_execution"],
        ))
        steps.append(next_step(
            title="Perform Analysis",
            description="Run the analysis to extract the requested insights, patterns, or statistics.",
            step_type="execute",
            effort="large",
            capabilities=["code_execution", "data_parse"],
        ))
        steps.append(next_step(
            title="Generate Report",
            description="Compile the findings into a clear, structured report with visualizations if applicable.",
            step_type="execute",
            effort="medium",
            capabilities=["document_generation"],
        ))
        steps.append(next_step(
            title="Deliver the Result",
            description="Present the analysis findings and report.",
            step_type="deliver",
            effort="small",
        ))

        return steps

    def _decompose_generic(self, intent: Intent) -> list[TaskStep]:
        """Decompose a generic/unknown task type."""
        steps = []
        counter = [0]

        def next_step(
            title: str,
            description: str,
            step_type: str = "execute",
            effort: str = "medium",
            capabilities: Optional[list[str]] = None,
        ) -> TaskStep:
            counter[0] += 1
            return TaskStep(
                id=f"step-{counter[0]}",
                title=title,
                description=description,
                step_type=step_type,
                order=counter[0],
                required_capabilities=capabilities or [],
                estimated_effort=effort,
            )

        steps.append(next_step(
            title="Understand the Request",
            description="Analyze the request to extract the goal, requirements, and definition of done.",
            step_type="understand",
            effort="small",
            capabilities=["intent_parsing"],
        ))
        steps.append(next_step(
            title="Plan the Approach",
            description="Determine the best way to accomplish the task given the available capabilities.",
            step_type="plan",
            effort="small",
            capabilities=["planning"],
        ))
        steps.append(next_step(
            title="Execute the Task",
            description="Perform the actual work to fulfill the request.",
            step_type="execute",
            effort="medium",
            capabilities=[],
        ))
        steps.append(next_step(
            title="Verify the Result",
            description="Check that the task was completed successfully.",
            step_type="verify",
            effort="small",
            capabilities=[],
        ))
        steps.append(next_step(
            title="Deliver the Result",
            description="Report the outcome to the user.",
            step_type="deliver",
            effort="trivial",
        ))

        return steps


_engine: Optional[TaskDecomposer] = None


def get_task_decomposer() -> TaskDecomposer:
    """Get the shared TaskDecomposer instance."""
    global _engine
    if _engine is None:
        _engine = TaskDecomposer()
    return _engine
