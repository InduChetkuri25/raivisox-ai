"""Intent Engine — converts natural language into structured objectives.

Turns "Build me a clinic appointment application" into a structured
goal with requirements, constraints, risk assessment, and definition of done.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class TaskType(str, Enum):
    """Categories of work RAIVISOX can perform."""

    SOFTWARE_BUILD = "software_build"
    RESEARCH = "research"
    FILE_OPERATION = "file_operation"
    DEBUGGING = "debugging"
    DATA_ANALYSIS = "data_analysis"
    DOCUMENT_GENERATION = "document_generation"
    INCREMENTAL_IMPROVEMENT = "incremental_improvement"
    EXPLORATION = "exploration"
    UNKNOWN = "unknown"


class RiskLevel(str, Enum):
    """Inherent risk of the requested task."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AmbiguityLevel(str, Enum):
    """How ambiguous the user's request is."""

    CLEAR = "clear"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Requirement:
    """A single requirement extracted from the user's request."""

    text: str
    category: str  # functional, non_functional, constraint, preference
    priority: str = "required"  # required, desired, optional


@dataclass
class DefinitionOfDone:
    """Objective criteria that determine when the task is complete."""

    criteria: list[str] = field(default_factory=list)
    verification_methods: list[str] = field(default_factory=list)
    acceptance_threshold: str = "All required criteria met"


@dataclass
class Intent:
    """Structured representation of a user's request.

    Produced by the Intent Engine from natural language input.
    """

    raw_input: str
    goal: str
    task_type: TaskType = TaskType.UNKNOWN
    requirements: list[Requirement] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    required_capabilities: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    ambiguity: AmbiguityLevel = AmbiguityLevel.CLEAR
    clarification_needed: list[str] = field(default_factory=list)
    definition_of_done: DefinitionOfDone = field(default_factory=DefinitionOfDone)
    created_at: datetime = field(default_factory=datetime.utcnow)


class IntentEngine:
    """Converts natural language into structured Intent objects.

    In production, this would call an LLM with structured output.
    For now, it provides a clean interface and heuristic-based parsing
    that can be replaced with model-backed parsing when credentials exist.
    """

    def __init__(self):
        self._capability_map: dict[str, list[str]] = {
            "software_build": [
                "code_generation",
                "filesystem_write",
                "terminal_execute",
                "test_execution",
            ],
            "research": [
                "web_search",
                "web_read",
                "document_parse",
            ],
            "debugging": [
                "code_read",
                "test_execution",
                "error_analysis",
            ],
            "file_operation": [
                "filesystem_read",
                "filesystem_write",
            ],
            "data_analysis": [
                "data_parse",
                "code_execution",
            ],
        }

    def parse(self, user_input: str, context: Optional[dict[str, Any]] = None) -> Intent:
        """Parse natural language input into a structured Intent.

        Args:
            user_input: The raw text from the user.
            context: Optional additional context (project, user prefs, etc.)

        Returns:
            A structured Intent object.
        """
        intent = Intent(
            raw_input=user_input,
            goal=self._extract_goal(user_input),
            context=context or {},
        )

        # Run analysis phases
        intent.task_type = self._classify_task_type(user_input, intent.goal)
        self._extract_requirements(intent, user_input)
        self._extract_constraints(intent, user_input)
        self._assess_risk(intent)
        self._assess_ambiguity(intent, user_input)
        self._derive_capabilities(intent)
        self._build_definition_of_done(intent)

        return intent

    def _extract_goal(self, user_input: str) -> str:
        """Extract a clear goal statement from the input."""
        cleaned = user_input.strip()

        # Remove common prefixes
        for prefix in [
            "RAIVISOX, ",
            "RAIVISOX ",
            "please ",
            "can you ",
            "could you ",
            "build me ",
            "create ",
        ]:
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break

        # Capitalize first letter
        if cleaned:
            cleaned = cleaned[0].upper() + cleaned[1:]

        return cleaned or user_input

    def _classify_task_type(self, user_input: str, goal: str) -> TaskType:
        """Classify what kind of task this is."""
        text = (user_input + " " + goal).lower()

        # Software build signals
        build_signals = [
            "build", "create", "make", "develop", "write",
            "application", "app", "website", "web app", "platform",
            "tool", "service", "api", "cli", "dashboard",
            "clone", "replicate", "implement",
        ]
        if any(s in text for s in build_signals):
            return TaskType.SOFTWARE_BUILD

        # Debugging signals
        debug_signals = [
            "fix", "debug", "error", "bug", "broken", "not working",
            "fail", "issue", "problem", "crash",
        ]
        if any(s in text for s in debug_signals):
            return TaskType.DEBUGGING

        # Research signals
        research_signals = [
            "research", "find", "look up", "investigate", "learn about",
            "what is", "how does", "explain", "compare", "review",
            "summary", "overview",
        ]
        if any(s in text for s in research_signals):
            return TaskType.RESEARCH

        # File operation signals
        file_signals = [
            "read", "write", "edit", "modify", "delete", "move",
            "copy", "rename", "organize", "convert", "merge",
            "file", "document", "pdf", "image", "video",
        ]
        if any(s in text for s in file_signals):
            return TaskType.FILE_OPERATION

        # Data analysis signals
        data_signals = [
            "analyze", "data", "dataset", "csv", "excel", "spreadsheet",
            "chart", "graph", "statistics", "report",
        ]
        if any(s in text for s in data_signals):
            return TaskType.DATA_ANALYSIS

        # Document generation
        doc_signals = [
            "write", "draft", "compose", "generate", "create",
            "email", "letter", "report", "article", "blog",
            "story", "script", "document",
        ]
        if any(s in text for s in doc_signals):
            return TaskType.DOCUMENT_GENERATION

        return TaskType.UNKNOWN

    def _extract_requirements(self, intent: Intent, user_input: str) -> None:
        """Extract requirements from the input."""
        text = user_input.lower()

        # Look for explicit requirements
        if "must" in text or "have to" in text or "needs to" in text:
            intent.requirements.append(
                Requirement(
                    text="User specified explicit requirements",
                    category="functional",
                    priority="required",
                )
            )

        # Look for technology preferences
        tech_hints = {
            "python": "Python",
            "typescript": "TypeScript",
            "javascript": "JavaScript",
            "react": "React",
            "next.js": "Next.js",
            "fastapi": "FastAPI",
            "django": "Django",
            "flask": "Flask",
            "postgres": "PostgreSQL",
            "sqlite": "SQLite",
            "docker": "Docker",
            "aws": "AWS",
            "azure": "Azure",
            "gcp": "Google Cloud",
        }
        for hint, tech in tech_hints.items():
            if hint in text:
                intent.requirements.append(
                    Requirement(
                        text=f"Use {tech}",
                        category="preference",
                        priority="desired",
                    )
                )

        # Look for platform targets
        if "web" in text or "browser" in text:
            intent.requirements.append(
                Requirement(
                    text="Web-based interface",
                    category="functional",
                    priority="required",
                )
            )
        if "mobile" in text or "ios" in text or "android" in text:
            intent.requirements.append(
                Requirement(
                    text="Mobile platform support",
                    category="functional",
                    priority="required",
                )
            )

        # If no requirements found, add a default
        if not intent.requirements:
            intent.requirements.append(
                Requirement(
                    text=f"Fulfill the request: {intent.goal}",
                    category="functional",
                    priority="required",
                )
            )

    def _extract_constraints(self, intent: Intent, user_input: str) -> None:
        """Extract constraints from the input."""
        text = user_input.lower()

        if "quickly" in text or "fast" in text or "urgently" in text:
            intent.constraints.append("Time-sensitive execution preferred")
        if "simply" in text or "simple" in text or "minimal" in text:
            intent.constraints.append("Prefer simplicity over features")
        if "production" in text or "deployed" in text:
            intent.constraints.append("Must be production-quality")

    def _assess_risk(self, intent: Intent) -> None:
        """Assess the inherent risk level of the task."""
        risk = RiskLevel.LOW

        if intent.task_type == TaskType.SOFTWARE_BUILD:
            risk = RiskLevel.MEDIUM
            if any(
                c in " ".join(intent.constraints).lower()
                for c in ["production", "deployed", "user-facing"]
            ):
                risk = RiskLevel.HIGH

        elif intent.task_type == TaskType.DEBUGGING:
            risk = RiskLevel.MEDIUM

        elif intent.task_type == TaskType.FILE_OPERATION:
            risk = RiskLevel.LOW
            if "delete" in " ".join(intent.constraints).lower():
                risk = RiskLevel.HIGH

        intent.risk_level = risk

    def _assess_ambiguity(self, intent: Intent, user_input: str) -> None:
        """Assess how ambiguous the request is and what's needed to clarify."""
        text = user_input.lower()
        ambiguity = AmbiguityLevel.CLEAR
        clarifications = []

        # Check for missing specifics in software builds
        if intent.task_type == TaskType.SOFTWARE_BUILD:
            if "database" not in text and "storage" not in text:
                # Not necessarily ambiguous — could be file-based
                pass
            if len(user_input.split()) < 5:
                ambiguity = AmbiguityLevel.MODERATE
                clarifications.append(
                    "Specify the scope and key features of the application"
                )

        # Check for missing targets
        if "for" not in text and "to" not in text:
            if ambiguity == AmbiguityLevel.CLEAR:
                ambiguity = AmbiguityLevel.MODERATE

        intent.ambiguity = ambiguity
        intent.clarification_needed = clarifications

    def _derive_capabilities(self, intent: Intent) -> None:
        """Derive the capabilities required to execute this task."""
        task_type = intent.task_type.value
        capabilities = self._capability_map.get(task_type, [])

        # Always need filesystem for most tasks
        if intent.task_type in (
            TaskType.SOFTWARE_BUILD,
            TaskType.DEBUGGING,
            TaskType.FILE_OPERATION,
        ):
            if "filesystem_read" not in capabilities:
                capabilities.append("filesystem_read")

        intent.required_capabilities = capabilities

    def _build_definition_of_done(self, intent: Intent) -> None:
        """Build an objective definition of done for this task."""
        dod = DefinitionOfDone()

        if intent.task_type == TaskType.SOFTWARE_BUILD:
            dod.criteria = [
                "Application code is generated and saved to the target location",
                "Application can be built without errors (where build system exists)",
                "Application runs and responds to basic interaction",
                "Core functionality requested by the user is implemented",
            ]
            dod.verification_methods = [
                "Build the application and check for compilation errors",
                "Run the application and verify it starts successfully",
                "Test the primary user flow end-to-end",
            ]

        elif intent.task_type == TaskType.DEBUGGING:
            dod.criteria = [
                "Root cause of the issue is identified",
                "Fix is applied to the relevant code",
                "Fix is verified by re-running the failing scenario",
                "No regressions introduced in related functionality",
            ]
            dod.verification_methods = [
                "Reproduce the original error and confirm it no longer occurs",
                "Run existing tests to check for regressions",
            ]

        elif intent.task_type == TaskType.RESEARCH:
            dod.criteria = [
                "Information is gathered from relevant sources",
                "Findings are summarized clearly",
                "Sources are cited where applicable",
            ]
            dod.verification_methods = [
                "Review the summary for completeness and accuracy",
            ]

        elif intent.task_type == TaskType.FILE_OPERATION:
            dod.criteria = [
                "The requested file operation is performed",
                "The result is verified (file exists, content is correct, etc.)",
            ]
            dod.verification_methods = [
                "Verify the file operation result directly",
            ]

        else:
            dod.criteria = [
                "The user's request is fulfilled to the best of the system's ability",
                "Results are delivered with clear explanation of what was done",
            ]
            dod.verification_methods = [
                "User confirms the result matches their expectation",
            ]

        intent.definition_of_done = dod


# Singleton instance
_engine: Optional[IntentEngine] = None


def get_intent_engine() -> IntentEngine:
    """Get the shared IntentEngine instance."""
    global _engine
    if _engine is None:
        _engine = IntentEngine()
    return _engine
