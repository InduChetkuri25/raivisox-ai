"""Definition of Done service.

Generates and manages objective success criteria for tasks.
Ensures every task has verifiable completion conditions.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class DodCategory(str, Enum):
    """Categories of definition-of-done criteria."""

    FUNCTIONAL = "functional"
    QUALITY = "quality"
    PERFORMANCE = "performance"
    SECURITY = "security"
    USABILITY = "usability"
    COMPATIBILITY = "compatibility"


class DodCriterion(str, Enum):
    """Standard criteria that can be applied to tasks."""

    CODE_GENERATED = "Code is generated and saved"
    BUILD_SUCCEEDS = "Application builds without errors"
    APPLICATION_RUNS = "Application starts and runs"
    CORE_FEATURES_WORK = "Core requested features are implemented and functional"
    TESTS_PASS = "Tests pass without failures"
    NO_REGATIONS = "No regressions in existing functionality"
    VERIFICATION_PASSED = "Independent verification confirms success"
    DOCUMENTATION_PROVIDED = "Instructions or documentation is provided"


@dataclass
class DodCriterionEntry:
    """A single criterion in the definition of done."""

    id: str
    text: str
    category: DodCategory
    verified: bool = False
    verification_method: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    checked_at: Optional[datetime] = None
    notes: str = ""


@dataclass
class DefinitionOfDone:
    """Complete definition of done for a task.

    Contains the criteria, verification methods, and tracking
    of which criteria have been met.
    """

    task_id: str
    goal: str
    criteria: list[DodCriterionEntry] = field(default_factory=list)
    verification_methods: list[str] = field(default_factory=list)
    acceptance_threshold: str = "All required criteria verified"
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

    @property
    def all_criteria_verified(self) -> bool:
        """Check if all criteria have been verified."""
        return all(c.verified for c in self.criteria) if self.criteria else False

    @property
    def verified_count(self) -> int:
        """Count of verified criteria."""
        return sum(1 for c in self.criteria if c.verified)

    @property
    def total_count(self) -> int:
        """Total number of criteria."""
        return len(self.criteria)

    @property
    def progress(self) -> float:
        """Completion progress as a float 0.0-1.0."""
        if not self.criteria:
            return 0.0
        return self.verified_count / self.total_count

    def mark_verified(self, criterion_id: str, evidence: Optional[dict[str, Any]] = None) -> bool:
        """Mark a criterion as verified.

        Args:
            criterion_id: The ID of the criterion to mark.
            evidence: Optional evidence supporting the verification.

        Returns:
            True if the criterion was found and marked, False otherwise.
        """
        for criterion in self.criteria:
            if criterion.id == criterion_id:
                criterion.verified = True
                criterion.checked_at = datetime.utcnow()
                if evidence:
                    criterion.evidence.append(evidence)
                return True
        return False

    def mark_failed(self, criterion_id: str, notes: str = "") -> bool:
        """Mark a criterion as failed (not verified).

        Args:
            criterion_id: The ID of the criterion.
            notes: Explanation of why it failed.

        Returns:
            True if found and marked, False otherwise.
        """
        for criterion in self.criteria:
            if criterion.id == criterion_id:
                criterion.verified = False
                criterion.notes = notes
                criterion.checked_at = datetime.utcnow()
                return True
        return False

    def to_summary(self) -> dict[str, Any]:
        """Get a summary dict for API responses."""
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "criteria": [
                {
                    "id": c.id,
                    "text": c.text,
                    "category": c.category.value,
                    "verified": c.verified,
                    "verification_method": c.verification_method,
                    "evidence_count": len(c.evidence),
                    "notes": c.notes,
                }
                for c in self.criteria
            ],
            "verification_methods": self.verification_methods,
            "acceptance_threshold": self.acceptance_threshold,
            "progress": self.progress,
            "verified_count": self.verified_count,
            "total_count": self.total_count,
            "all_verified": self.all_criteria_verified,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class DodGenerator:
    """Generates Definitions of Done for different task types.

    Creates objective, verifiable criteria based on what was asked.
    """

    def __init__(self):
        self._criterion_counter = 0

    def generate(self, task_id: str, goal: str, task_type: str, requirements: list[dict]) -> DefinitionOfDone:
        """Generate a definition of done for a task.

        Args:
            task_id: The task identifier.
            goal: The task goal.
            task_type: The type of task (software_build, debugging, etc.)
            requirements: List of requirement dicts from the intent.

        Returns:
            A DefinitionOfDone with criteria for this task.
        """
        self._criterion_counter = 0

        dod = DefinitionOfDone(
            task_id=task_id,
            goal=goal,
        )

        # Add standard criteria based on task type
        if task_type == "software_build":
            self._add_criteria_for_software_build(dod, requirements)
        elif task_type == "debugging":
            self._add_criteria_for_debugging(dod, requirements)
        elif task_type == "research":
            self._add_criteria_for_research(dod, requirements)
        elif task_type == "file_operation":
            self._add_criteria_for_file_operation(dod, requirements)
        else:
            self._add_generic_criteria(dod, task_type)

        # Add criteria from explicit requirements
        self._add_requirement_criteria(dod, requirements)

        # Set verification methods
        self._set_verification_methods(dod, task_type)

        return dod

    def _add_criteria_for_software_build(self, dod: DefinitionOfDone, requirements: list[dict]) -> None:
        """Add criteria specific to software builds."""
        self._add(dod, DodCriterion.CODE_GENERATED, DodCategory.FUNCTIONAL,
                   "Application source code has been generated and saved to the project directory")
        self._add(dod, DodCriterion.BUILD_SUCCEEDS, DodCategory.QUALITY,
                   "The application builds without compilation or syntax errors")
        self._add(dod, DodCriterion.APPLICATION_RUNS, DodCategory.FUNCTIONAL,
                   "The application starts up and is accessible")
        self._add(dod, DodCriterion.CORE_FEATURES_WORK, DodCategory.FUNCTIONAL,
                   "The core features requested by the user are implemented and operational")
        self._add(dod, DodCriterion.VERIFICATION_PASSED, DodCategory.QUALITY,
                   "An independent verification confirms the application meets the definition of done")

        # Add quality criteria if it's a production request
        req_text = " ".join(r.get("text", "").lower() for r in requirements)
        if "production" in req_text or "deployed" in req_text:
            self._add(dod, DodCriterion.TESTS_PASS, DodCategory.QUALITY,
                       "Automated tests pass successfully")
            self._add(dod, DodCriterion.NO_REGATIONS, DodCategory.QUALITY,
                       "Existing functionality is not negatively affected")

    def _add_criteria_for_debugging(self, dod: DefinitionOfDone, requirements: list[dict]) -> None:
        """Add criteria specific to debugging tasks."""
        self._add(dod, "root_cause_identified", DodCategory.FUNCTIONAL,
                   "The root cause of the issue has been identified and documented")
        self._add(dod, DodCriterion.CODE_GENERATED, DodCategory.FUNCTIONAL,
                   "The fix has been applied to the relevant code")
        self._add(dod, "issue_resolved", DodCategory.FUNCTIONAL,
                   "The original error or issue no longer occurs when the scenario is re-run")
        self._add(dod, DodCriterion.NO_REGATIONS, DodCategory.QUALITY,
                   "The fix has not introduced regressions in related functionality")

    def _add_criteria_for_research(self, dod: DefinitionOfDone, requirements: list[dict]) -> None:
        """Add criteria specific to research tasks."""
        self._add(dod, "information_gathered", DodCategory.FUNCTIONAL,
                   "Relevant information has been gathered from appropriate sources")
        self._add(dod, "findings_summarized", DodCategory.FUNCTIONAL,
                   "Findings are clearly summarized and organized")
        self._add(dod, "sources_cited", DodCategory.QUALITY,
                   "Sources are cited where applicable")

    def _add_criteria_for_file_operation(self, dod: DefinitionOfDone, requirements: list[dict]) -> None:
        """Add criteria specific to file operations."""
        self._add(dod, "operation_performed", DodCategory.FUNCTIONAL,
                   "The requested file operation has been performed")
        self._add(dod, "result_verified", DodCategory.QUALITY,
                   "The result has been verified (file exists, content is correct, etc.)")

    def _add_generic_criteria(self, dod: DefinitionOfDone, task_type: str) -> None:
        """Add generic criteria for unknown task types."""
        self._add(dod, "request_fulfilled", DodCategory.FUNCTIONAL,
                   f"The user's request has been fulfilled to the best of the system's ability")
        self._add(dod, "result_delivered", DodCategory.FUNCTIONAL,
                   "Results are delivered with a clear explanation of what was done")

    def _add_requirement_criteria(self, dod: DefinitionOfDone, requirements: list[dict]) -> None:
        """Add criteria derived from explicit requirements."""
        for req in requirements:
            priority = req.get("priority", "desired")
            if priority == "required":
                text = req.get("text", "")
                if text and len(text) > 5:
                    self._add(dod, f"req_{len(dod.criteria)}", DodCategory.FUNCTIONAL,
                               f"Requirement met: {text}")

    def _set_verification_methods(self, dod: DefinitionOfDone, task_type: str) -> None:
        """Set verification methods based on task type."""
        if task_type == "software_build":
            dod.verification_methods = [
                "Build the application and check for errors",
                "Run the application and verify it starts",
                "Test the primary user flow end-to-end",
                "Review the code structure and implementation",
            ]
        elif task_type == "debugging":
            dod.verification_methods = [
                "Reproduce the original error and confirm it no longer occurs",
                "Run existing tests to check for regressions",
                "Review the fix for correctness",
            ]
        elif task_type == "research":
            dod.verification_methods = [
                "Review the summary for completeness and accuracy",
                "Cross-check key claims against sources",
            ]
        elif task_type == "file_operation":
            dod.verification_methods = [
                "Verify the file operation result directly",
            ]
        else:
            dod.verification_methods = [
                "Review the result against the original request",
            ]

    def _add(self, dod: DefinitionOfDone, key: str, category: DodCategory, text: str) -> DodCriterionEntry:
        """Add a criterion to the definition of done."""
        self._criterion_counter += 1
        criterion = DodCriterionEntry(
            id=f"criterion-{self._criterion_counter}",
            text=text,
            category=category,
            verification_method="Manual or automated verification as specified",
        )
        dod.criteria.append(criterion)
        return criterion


_engine: Optional[DodGenerator] = None


def get_dod_generator() -> DodGenerator:
    """Get the shared DodGenerator instance."""
    global _engine
    if _engine is None:
        _engine = DodGenerator()
    return _engine
