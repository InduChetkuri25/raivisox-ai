"""RAIVISOX AI API - Main Application."""

import json as _json
import logging as _logging
import os as _os
import re as _re
from contextlib import asynccontextmanager
from typing import Annotated, AsyncGenerator, Optional
import uuid as _uuid
from datetime import datetime
from sqlalchemy import update

from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
import io

from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.config import provider_configured, settings, validate_settings
from app.database import async_session_factory, close_db, get_db, init_db
from app import database as database_pkg
from app.services import artifact_cleanup
from app.auth.router import router as auth_router
from app.auth.service import get_current_user
from app.tasks.router import router as tasks_router
from app.tasks.schemas import (
    StepStatus,
    TaskCreate,
    TaskStatus,
    TaskStepCreate,
    TaskStepUpdate,
    TaskUpdate,
)
from app.models.task import Task, TaskOutcome
from app.models.execution_run import ExecutionRun, ExecutionRunStatus
from services.security.audit_trail import AuditTrail
from app.tasks import service as task_service
from app.models.task_event import EventType
from services.intent.engine import get_intent_engine
from services.decomposer.decomposer import get_task_decomposer
from services.dod.dod import get_dod_generator
from services.dod.dod_checks import run_module_checks_async as run_dod_checks_async
from services.dod.dod_checks import summarize as summarize_dod
from services.planner.planner import get_planner
from services.router.router import get_router
from services.execution.task_graph import (
    ExecutionStatus,
    ParallelExecutor,
    TaskGraph,
    TaskNode,
)
from services.execution.terminal_sandbox import TerminalSandbox
from services.execution.observation import ObservationEngine
from services.verification.verification_engine import VerificationEngine, Verdict
from services.recovery.recovery_engine import RecoveryEngine, RecoveryStrategy
from services.computer.browser_controller import BrowserController
from services.computer.computer_control import ComputerControl
from services.security.security_kernel import (
    ActionContext,
    Permission,
    SecurityKernel,
)
from services.security.permission_system import PermissionSystem, Policy, Role
from services.scheduler.task_scheduler import (
    ScheduledTask,
    ScheduleResult,
    TaskScheduler,
)
from services.benchmark.bench_runner import (
    BenchmarkCase,
    BenchmarkResult,
    RAIVISOXBenchRunner,
)
from services.observability.metrics_collector import MetricsCollector
from services.observability.log_aggregator import LogAggregator
from services.observability.trace_manager import TraceManager
from services.memory.experience_memory import (
    ExperienceMemory,
    LearnedStrategy,
    StrategyLearner,
)

from integrations.adapters import (
    AdapterContext,
    AdapterRegistry,
    CodegenAdapter,
    FileReadAdapter,
    FileWriteAdapter,
    FinalizeAdapter,
    HttpGetAdapter,
    PythonExecAdapter,
    ShellExecAdapter,
    TextIntelAdapter,
    TextTransformAdapter,
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=True)

# ---- Structured operational logging ---------------------------------------
# Operational events log as stable ``key=value`` fields (task_id,
# execution_run_id, status, outcome, duration_s) so production log
# pipelines can parse them. NEVER log secrets, tokens, passwords or
# request bodies — exception text is allowed (server-side only).
logger = _logging.getLogger("raivisox.api")

# Absolute-path scrubber for user-visible persisted error text: internal
# filesystem paths (Windows drive paths or POSIX absolute paths) must not
# reach API responses / task error payloads.
_PATH_RE = _re.compile(
    r"(?:[A-Za-z]:\\[^\s'\"<>|]+)|(?:/(?:[\w.\-]+/)+[\w.\-]+)"
)


def _scrub_internal_paths(text: str) -> str:
    """Replace absolute filesystem paths in ``text`` with ``<path>``."""
    try:
        return _PATH_RE.sub("<path>", text)
    except Exception:  # noqa: BLE001 — scrubbing must never break persistence
        return text


def _display_path(path: str) -> str:
    """Workspace-relative path for user-facing documents.

    Files inside the task workspace are shown relative to it (useful to
    the user, reveals nothing about the server layout); anything outside
    degrades to its basename.
    """
    try:
        root = _os.path.realpath(TASK_WORKSPACE)
        rp = _os.path.realpath(path)
        if rp.startswith(root + _os.sep):
            return _os.path.relpath(rp, root)
        return _os.path.basename(rp)
    except Exception:  # noqa: BLE001 — display only
        return "<path>"


async def get_current_user_dep(
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Depends(oauth2_scheme)],
):
    """Resolve the current user from the Authorization: Bearer header."""
    from app.auth.service import decode_token, get_user_by_id

    token_data = decode_token(token)
    if not token_data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = await get_user_by_id(db, token_data.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


_audit_flusher_task: "asyncio.Task[None] | None" = None
# Background pipelines started by async execute (cancels them on shutdown).
_background_pipelines: set = set()
_cleanup_worker_task: "asyncio.Task[None] | None" = None


async def _artifact_cleanup_loop() -> None:
    """Scheduled artifact-retention passes (one worker per process).

    Started ONLY when ARTIFACT_RETENTION_DAYS > 0 — at 0 the loop does not
    exist and bytes are kept forever. Never blocks API requests (own task,
    own DB session) and never crashes the API: every pass failure is logged
    and retried next interval. Logs safe statistics only (counts, no paths).
    """
    interval = max(1, int(settings.artifact_cleanup_interval_seconds))
    while True:
        await asyncio.sleep(interval)
        try:
            stats = await artifact_cleanup.run_cleanup_with_own_session(
                TASK_WORKSPACE, settings
            )
            logger.info(
                "artifact cleanup pass scanned=%(scanned)d deleted=%(deleted)d "
                "bytes_freed=%(bytes_freed)d skipped_current_path=%(skipped_current_path)d "
                "skipped_modified=%(skipped_file_modified)d already_missing=%(already_missing)d "
                "unsafe=%(skipped_path_unsafe)d orphans=%(orphans_deleted)d "
                "orphan_bytes=%(orphan_bytes_freed)d errors=%(errors)d",
                stats,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed pass must not kill the API
            logger.warning("artifact cleanup pass failed, will retry next interval: %s", exc)


async def _audit_flush_loop() -> None:
    """Periodically persist queued audit events so evidence survives restarts."""
    trail = AuditTrail.instance()
    while True:
        await asyncio.sleep(2.0)
        if trail.pending_count() == 0:
            continue
        try:
            async with async_session_factory() as db:
                await trail.flush_pending(db)
        except Exception as exc:  # noqa: BLE001 — events stay queued; retry next tick
            logger.warning("audit flush deferred: %s", exc)


async def _interruption_stage(db: AsyncSession, task: Task) -> str:
    """Derive WHERE a pipeline was interrupted from persisted evidence only.

    Uses the task's own transition events and status — never a guess. Used
    by the startup sweep so an orphaned task's failure names its stage.
    """
    from datetime import datetime
    from sqlalchemy import select, update

    from app.models.task_event import TaskEvent

    if task.status == TaskStatus.RECOVERING.value:
        return "recovery"
    if task.status == TaskStatus.VERIFYING.value:
        return "verification"
    # EXECUTING: find the latest step_started with no later completion for
    # the same step — that step was in flight when the process died.
    events = (
        (
            await db.execute(
                select(TaskEvent)
                .where(TaskEvent.task_id == task.id)
                .order_by(TaskEvent.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    started: set[str] = set()
    finished: set[str] = set()
    for ev in events:
        sid = (ev.payload or {}).get("step_id")
        if not sid:
            continue
        if ev.event_type == EventType.STEP_STARTED.value:
            # A (re-)start re-arms the step: any earlier completion belongs
            # to a previous pass (e.g. post-recovery regeneration).
            started.add(sid)
            finished.discard(sid)
        elif ev.event_type in (EventType.STEP_COMPLETED.value, EventType.STEP_FAILED.value, EventType.STEP_SKIPPED.value):
            finished.add(sid)
    in_flight = sorted(started - finished)
    if in_flight:
        return f"execution (step {in_flight[-1]} in flight)"
    if started:
        return "execution (between steps)"
    return "execution (pipeline start)"


async def _close_open_execution_runs(
    task_id: str,
    *,
    status: str,
    outcome: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Close every still-running ExecutionRun row for a task.

    Crash-path backstop (background pipeline crash, sync execute crash):
    an execution_run may never stay ``running`` — a run that did not reach
    its terminal block is closed honestly as failed. Uses its own session
    (the failing pipeline's session may be unusable) and only matches
    RUNNING rows, so it can never overwrite a run the terminal block
    already finished. Exactly-once by construction.
    """
    try:
        async with async_session_factory() as session:
            await session.execute(
                update(ExecutionRun)
                .where(
                    ExecutionRun.task_id == task_id,
                    ExecutionRun.status == ExecutionRunStatus.RUNNING,
                )
                .values(
                    finished_at=datetime.utcnow(),
                    status=status,
                    outcome=outcome,
                    error=(error or None)[:2000] if error else None,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — observability, never control flow
        logger.error("execution_run close failed task_id=%s error=%s", task_id, exc)


async def _reset_orphaned_running_tasks() -> None:
    """Fail tasks left EXECUTING/RECOVERING/VERIFYING by a previous process.

    Determination (re-evaluated after fine-grained persistence): each node
    transition now updates its TaskStep row in the SAME transaction as its
    event, so an orphan's completed/failed/in-flight steps ARE observable.
    That observability is diagnostics, not a resume guarantee: adapters are
    side-effectful (a step marked executing may have half-applied writes),
    worker dependency wiring lives in the in-memory graph, and recovery
    attempt bookkeeping is not persisted. Automatic mid-pipeline resume
    therefore remains UNSUPPORTED. Each orphan is marked honestly FAILED
    with its interruption stage (derived from its own persisted events),
    and an explicit safe re-run is available via execute {"resume_mode":
    true} (fresh complete run; cannot fake success).
    """
    from sqlalchemy import select

    async with async_session_factory() as db:
        stuck = (
            (
                await db.execute(
                    select(Task).where(
                        Task.status.in_(
                            [
                                TaskStatus.EXECUTING.value,
                                TaskStatus.RECOVERING.value,
                                TaskStatus.VERIFYING.value,
                            ]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for task in stuck:
            stage = await _interruption_stage(db, task)
            message = f"execution interrupted by server restart during {stage}"
            task.status = TaskStatus.FAILED.value
            task.outcome = TaskOutcome.FAILED.value
            task.error = {"message": message}
            # First-Class Execution Runs: close any run row this orphan left
            # open when the process died — a run may never stay running.
            await db.execute(
                update(ExecutionRun)
                .where(
                    ExecutionRun.task_id == task.id,
                    ExecutionRun.status == ExecutionRunStatus.RUNNING,
                )
                .values(
                    finished_at=datetime.utcnow(),
                    status=ExecutionRunStatus.FAILED,
                    outcome=TaskOutcome.FAILED.value,
                    error=message[:2000],
                )
            )
            await task_service.create_task_event(
                db,
                task_id=task.id,
                event_type=EventType.ERROR.value,
                payload={"error": message, "stage": stage, "startup_sweep": True},
            )
        if stuck:
            await db.commit()
            logger.warning("startup sweep interrupted_tasks=%d", len(stuck))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    global _audit_flusher_task, _cleanup_worker_task
    # Ensure operational logs actually reach a handler: uvicorn does not
    # configure the root logger, so without this, INFO records (pipeline
    # terminal lines, lifecycle) would silently vanish in production.
    # Under pytest the logging plugin already owns the root handlers.
    _root_logger = _logging.getLogger()
    _level = getattr(_logging, (settings.log_level or "INFO").upper(), _logging.INFO)
    if not _root_logger.handlers:
        _logging.basicConfig(level=_level)
    elif _root_logger.level == _logging.NOTSET or _root_logger.level > _level:
        _root_logger.setLevel(_level)
    # Startup — fail fast on unsafe production configuration. Error text
    # names environment VARIABLES only; no secret value is ever echoed.
    validate_settings(settings)
    if settings.environment != "production" and settings.secret_key.startswith(
        "dev-secret-key"
    ):
        # Tokens are signed with a PUBLIC fallback key — anyone could mint
        # valid credentials. Local/dev convenience only; production refuses
        # the known default outright (validate_settings). Warned loudly so
        # local preview users still see it.
        logger.warning(
            "SECURITY WARNING: SECRET_KEY is the built-in development "
            "default. Set a real SECRET_KEY before exposing this API "
            "beyond localhost."
        )
    logger.info("starting service=%r version=%s", settings.app_name, settings.app_version)
    await init_db()
    await _reset_orphaned_running_tasks()
    await _backfill_task_artifacts()
    logger.info("database initialized")
    _audit_flusher_task = asyncio.create_task(_audit_flush_loop())
    # Artifact retention: one cleanup worker per process, ONLY when a
    # retention window is configured (0 = keep forever, no worker at all).
    if settings.artifact_retention_days > 0:
        _cleanup_worker_task = asyncio.create_task(_artifact_cleanup_loop())
        logger.info(
            "artifact retention worker started retention_days=%d interval_seconds=%d",
            settings.artifact_retention_days,
            settings.artifact_cleanup_interval_seconds,
        )
    yield
    # Shutdown — graceful: cancel in-flight background pipelines first so
    # they are not destroyed mid-write when the event loop closes. Any run
    # they leave open is closed honestly by the startup sweep on next boot.
    logger.info("shutting down")
    if _background_pipelines:
        for _t in list(_background_pipelines):
            _t.cancel()
        try:
            # Snapshot: done-callbacks mutate the set while we wait.
            await asyncio.wait(list(_background_pipelines), timeout=10)
        except Exception:  # noqa: BLE001 — shutdown must not raise
            pass
    if _audit_flusher_task is not None:
        _audit_flusher_task.cancel()
        try:
            await _audit_flusher_task
        except asyncio.CancelledError:
            pass
    if _cleanup_worker_task is not None:
        _cleanup_worker_task.cancel()
        try:
            await _cleanup_worker_task
        except asyncio.CancelledError:
            pass
    try:
        async with async_session_factory() as db:
            await AuditTrail.instance().flush_pending(db)
    except Exception:  # noqa: BLE001 — best-effort final flush during shutdown
        pass
    await close_db()
    logger.info("database connections closed")


# Create FastAPI app
# In production the interactive API docs/openapi schema are disabled
# (they are development-only surfaces that advertise every endpoint).
_IS_PROD = settings.environment == "production"
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Next-Gen Adaptive Digital Intelligence - From Intent to Verified Outcome",
    lifespan=lifespan,
    docs_url=None if _IS_PROD else "/docs",
    redoc_url=None if _IS_PROD else "/redoc",
    openapi_url=None if _IS_PROD else "/openapi.json",
)

# Add CORS middleware
# SECURITY: when no origins are configured the API must NOT combine the
# permissive "*" origin list with allow_credentials=True (browsers refuse
# that combination anyway, and any origin could then call the API with
# credentials). Default to the documented local dev origins instead.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Host-header hardening: when ALLOWED_HOSTS is configured (required in
# production), reject requests for any other Host header. Empty list =
# disabled, preserving current local behavior.
if settings.allowed_hosts:
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts)
    )

app.include_router(auth_router)
app.include_router(tasks_router)

# Phase 3-8 service singletons
task_graph = TaskGraph()
parallel_executor = ParallelExecutor(max_concurrent=4)
terminal_sandbox = TerminalSandbox()
observation_engine = ObservationEngine()
verification_engine = VerificationEngine()
recovery_engine = RecoveryEngine()
browser_controller = BrowserController(headless=True)
computer_control = ComputerControl()
security_kernel = SecurityKernel()
permission_system = PermissionSystem()
scheduler = TaskScheduler()
bench_runner = RAIVISOXBenchRunner()
metrics_collector = MetricsCollector()
log_aggregator = LogAggregator()
trace_manager = TraceManager()
experience_memory = ExperienceMemory()
strategy_learner = StrategyLearner(experience_memory)

# 'system' actor holds ADMIN in the permission system.
permission_system.assign_role(security_kernel, "system", Role.ADMIN)

# The pipeline executes steps through kernel-gated adapters. Filesystem
# and shell access are clamped to TASK_WORKSPACE; the pipeline actor holds
# only the permissions its adapters declare.
PIPELINE_ACTOR = "pipeline-service"
TASK_WORKSPACE = _os.path.join(_os.getcwd(), "workspaces", "tasks")


def compute_outcome(
    final_status,
    verification_verdict,
    dod_summary: dict | None,
    simulated_count: int,
) -> str | None:
    """Honest-Outcome classification: the verification truth behind a status.

    verified              — execution PASSED and the Definition-of-Done stage
                            ran and was satisfied and every step used a real
                            adapter (zero simulated steps).
    completed_unverified  — the task finished "successfully" but success was
                            NOT independently confirmed: either the DoD stage
                            abstained (no derivable criterion), or simulated
                            steps participated in the result.
    failed                — the task failed verification or exhausted recovery.
    None                  — states with no outcome yet (created, executing,
                            recovering, cancelled).

    The headline rule of this pipeline — never report success without
    evidence — applies to the OUTCOME field: execution success alone is
    never "verified".
    """
    if final_status == TaskStatus.COMPLETED:
        verified = (
            verification_verdict == Verdict.PASSED
            and bool(dod_summary)
            and bool(dod_summary.get("applicable"))
            and dod_summary.get("satisfied") is True
            and simulated_count == 0
        )
        return (
            TaskOutcome.VERIFIED.value
            if verified
            else TaskOutcome.COMPLETED_UNVERIFIED.value
        )
    if final_status == TaskStatus.PARTIALLY_COMPLETED:
        return TaskOutcome.COMPLETED_UNVERIFIED.value
    if final_status == TaskStatus.FAILED:
        return TaskOutcome.FAILED.value
    return None

adapter_registry = AdapterRegistry(security_kernel)
adapter_registry.register(FileReadAdapter(TASK_WORKSPACE))
adapter_registry.register(FileWriteAdapter(TASK_WORKSPACE))
adapter_registry.register(ShellExecAdapter(TASK_WORKSPACE))
adapter_registry.register(PythonExecAdapter(TASK_WORKSPACE))
adapter_registry.register(TextTransformAdapter())
adapter_registry.register(HttpGetAdapter())
adapter_registry.register(TextIntelAdapter())
adapter_registry.register(CodegenAdapter(TASK_WORKSPACE))
# Default adapter: steps that declare NO capabilities get the finalize
# adapter, which composes the deliverable from real dependency outputs and
# writes it to a real file in the workspace. No pipeline step is simulated.
adapter_registry.register(FinalizeAdapter(TASK_WORKSPACE))

# Grant the pipeline actor exactly the permissions its real adapters need.
for _perm in (
    "read_files",
    "write_files",
    "execute_terminal",
    "execute_code",
    "network_request",
):
    security_kernel.grant_permission(PIPELINE_ACTOR, Permission(_perm))


@app.get("/api/v1/intent/parse")
async def parse_intent(
    user_input: str,
    context: dict | None = None,
    current_user: Annotated[dict, Depends(get_current_user_dep)] = None,
):
    """Parse a user's natural language input into a structured Intent.

    This is the entry point for the Intent Engine. It converts free-form
    text into a structured goal with requirements, constraints, risk
    assessment, and definition of done. Requires authentication.
    """
    engine = get_intent_engine()
    intent = engine.parse(user_input, context)

    return {
        "goal": intent.goal,
        "task_type": intent.task_type.value,
        "requirements": [
            {"text": r.text, "category": r.category, "priority": r.priority}
            for r in intent.requirements
        ],
        "constraints": intent.constraints,
        "required_capabilities": intent.required_capabilities,
        "risk_level": intent.risk_level.value,
        "ambiguity": intent.ambiguity.value,
        "clarification_needed": intent.clarification_needed,
        "definition_of_done": {
            "criteria": intent.definition_of_done.criteria,
            "verification_methods": intent.definition_of_done.verification_methods,
            "acceptance_threshold": intent.definition_of_done.acceptance_threshold,
        },
        "analyzed_at": intent.created_at.isoformat(),
    }


@app.post("/api/v1/tasks/{task_id}/decompose")
async def decompose_task(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[dict, Depends(get_current_user_dep)],
):
    """Decompose a task into actionable steps.

    Takes a task and produces a list of discrete, verifiable steps
    that can be executed by the system.
    """
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    decomposer = get_task_decomposer()
    steps = decomposer.decompose(intent=get_intent_engine().parse(task.title))

    return {
        "task_id": task_id,
        "steps": [
            {
                "id": s.id,
                "title": s.title,
                "description": s.description,
                "step_type": s.step_type,
                "order": s.order,
                "dependencies": s.dependencies,
                "required_capabilities": s.required_capabilities,
                "estimated_effort": s.estimated_effort,
                "can_parallelize": s.can_parallelize,
            }
            for s in steps
        ],
    }


@app.post("/api/v1/tasks/{task_id}/plan")
async def plan_task(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[dict, Depends(get_current_user_dep)],
):
    """Generate an execution plan for a task.

    Evaluates different strategies and selects the best one based on
    success probability, speed, cost, reliability, risk, and available
    capabilities.
    """
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    intent = get_intent_engine().parse(task.title)
    planner = get_planner()
    plan = planner.plan(
        task_id=task_id,
        goal=intent.goal,
        task_type=intent.task_type.value,
        required_capabilities=intent.required_capabilities,
        risk_level=intent.risk_level.value,
        ambiguity=intent.ambiguity.value,
    )

    return {
        "task_id": task_id,
        "strategy": {
            "id": plan.strategy.id,
            "name": plan.strategy.name,
            "description": plan.strategy.description,
            "approach": plan.strategy.approach.value,
            "estimated_speed": plan.strategy.estimated_speed.value,
            "estimated_reliability": plan.strategy.estimated_reliability.value,
            "estimated_cost": plan.strategy.estimated_cost.value,
            "confidence": plan.strategy.confidence,
            "rationale": plan.strategy.rationale,
        },
        "steps": [
            {
                "id": s.id,
                "order": s.order,
                "title": s.title,
                "description": s.description,
                "expected_output": s.expected_output,
                "capability_required": s.capability_required,
                "can_parallel_with": s.can_parallel_with,
                "status": s.status,
            }
            for s in plan.steps
        ],
        "total_steps": plan.total_steps,
        "parallelizable_steps": plan.parallelizable_steps,
    }


@app.get("/api/v1/models")
async def list_models(
    current_user: Annotated[dict, Depends(get_current_user_dep)] = None,
):
    """List available AI models and their capabilities. Requires authentication."""
    router = get_router()

    return {"models": router.get_available_models(), "tools": router.get_available_tools()}


@app.get("/api/v1/audit")
async def get_audit_trail(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[dict, Depends(get_current_user_dep)],
    limit: int = Query(100, ge=1, le=500),
    action: str | None = Query(None),
    allowed: bool | None = Query(None),
):
    """Read the durable audit trail (restart-safe).

    Requires the admin permission — the audit trail is security evidence,
    not a public log. Pending in-memory events are flushed first so the
    caller's own request is visible.
    """
    decision = security_kernel.authorize_and_log(
        actor_id=str(getattr(current_user, "id", current_user)),
        action="api:view_audit",
        resource="audit",
        permissions=["admin"],
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin permission required to read the audit trail",
        )
    trail = AuditTrail.instance()
    await trail.flush_pending(db)
    events = await trail.read_from_db(db, action=action, allowed=allowed, limit=limit)
    return {"events": events, "count": len(events)}


@app.post("/api/v1/admin/artifacts/cleanup")
async def admin_artifact_cleanup(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[dict, Depends(get_current_user_dep)],
):
    """Manually trigger one artifact-retention pass (admin only).

    Same policy as the scheduled worker: only expired NON-current artifact
    bytes inside the workspace root are deleted; registry metadata is never
    removed. No path input is accepted. Returns safe statistics (counts
    only — no paths, no filenames). Same 403-and-audit behavior as the
    audit endpoint for non-admin callers; retention=0 is an honest no-op
    ("retention_disabled": true), not an error.
    """
    decision = security_kernel.authorize_and_log(
        actor_id=str(getattr(current_user, "id", current_user)),
        action="api:admin_artifact_cleanup",
        resource="artifacts",
        permissions=["admin"],
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin permission required to trigger artifact cleanup",
        )
    try:
        stats = await artifact_cleanup.run_cleanup_with_own_session(
            TASK_WORKSPACE, settings
        )
    except Exception as exc:  # noqa: BLE001 — stable message, no internals
        logger.error("manual artifact cleanup failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="artifact cleanup failed; check server logs",
        )
    return {"stats": stats}


@app.post("/api/v1/tasks/{task_id}/route")
async def route_task(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[dict, Depends(get_current_user_dep)],
):
    """Route a task to the appropriate model and tools.

    Selects the best AI model and tool combination for the task
    based on requirements, risk level, and available capabilities.
    """
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    intent = get_intent_engine().parse(task.title)
    router = get_router()
    decision = router.route(
        task_id=task_id,
        task_type=intent.task_type.value,
        required_capabilities=intent.required_capabilities,
        risk_level=intent.risk_level.value,
    )

    return {
        "task_id": task_id,
        "selected_model": (
            {
                "id": decision.selected_model.id,
                "name": decision.selected_model.name,
                "provider": decision.selected_model.provider,
                "category": decision.selected_model.category.value,
            }
            if decision.selected_model
            else None
        ),
        "selected_tools": [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "category": t.category.value,
                "permissions": t.permissions,
                "risk_level": t.risk_level,
            }
            for t in decision.selected_tools
        ],
        "rationale": decision.rationale,
        "alternatives": decision.alternatives,
    }


@app.post("/api/v1/tasks/{task_id}/definition-of-done")
async def generate_dod(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[dict, Depends(get_current_user_dep)],
):
    """Generate a definition of done for a task.

    Creates objective, verifiable success criteria based on the
    task type and requirements.
    """
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    intent = get_intent_engine().parse(task.title)
    dod_generator = get_dod_generator()
    dod = dod_generator.generate(
        task_id=task_id,
        goal=intent.goal,
        task_type=intent.task_type.value,
        requirements=[
            {"text": r.text, "category": r.category, "priority": r.priority}
            for r in intent.requirements
        ],
    )

    return dod.to_summary()


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle all unhandled exceptions."""
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal server error",
            "detail": str(exc) if settings.debug else "An unexpected error occurred",
        },
    )


@app.get("/api/v1/tasks/{task_id}/artifacts")
async def list_task_artifacts(
    current_user: Annotated[dict, Depends(get_current_user_dep)],
    task_id: str,
    db: AsyncSession = Depends(get_db),
    run: str | None = None,
):
    """List artifacts a task's pipeline actually wrote (owner-scoped).

    Primary source: the task_artifacts registry. Legacy fallback: step
    results (pre-registry tasks), merged deterministically.

    ``?run=<run_id>`` scopes the listing to one execution run's registered
    rows (current AND superseded-within-run, with honest state flags). An
    unknown run — including a run_id belonging to ANOTHER task — is an
    honest 404; it never falls back to another run's artifacts.
    """
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if run is not None:
        # First-Class Execution Runs: the filter validates against the
        # durable run rows, scoped by BOTH task_id and run_id — a foreign
        # task's run (or any unknown id) is an honest 404. Legacy hex
        # run_ids from before Execution Runs keep working through the
        # artifact rows themselves.
        from sqlalchemy import select as _select

        run_row = (
            await db.execute(
                _select(ExecutionRun).where(
                    ExecutionRun.task_id == task_id, ExecutionRun.id == run
                )
            )
        ).scalars().first()
        rows = await _registry_rows_for_run_async(db, task_id, run)
        if run_row is None and not rows:
            raise HTTPException(status_code=404, detail="Unknown run for this task")
        arts: list[dict] = []
        for i, r in enumerate(rows):
            a = _artifact_public(r)
            a["index"] = i
            a["run_id"] = r.run_id
            a["is_current"] = r.is_current
            a["superseded_at"] = r.superseded_at.isoformat() if r.superseded_at else None
            arts.append(a)
        return {"task_id": task_id, "run_id": run, "artifacts": arts}
    rows = await _registry_rows_for_task_async(db, task_id)
    arts = []
    for i, r in enumerate(rows):
        a = _artifact_public(r)
        a["index"] = i
        arts.append(a)
    if not rows:
        # Legacy fallback for tasks executed before the registry existed.
        for j, a in enumerate(_collect_task_artifacts(task)):
            legacy = {k: v for k, v in a.items() if k != "path"}
            legacy["index"] = j
            arts.append(legacy)
    return {"task_id": task_id, "artifacts": arts}


@app.get("/api/v1/tasks/{task_id}/artifacts/history")
async def task_artifact_history(
    current_user: Annotated[dict, Depends(get_current_user_dep)],
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Factual persisted artifact history, grouped by FIRST-CLASS run.

    Run boundaries are authoritative execution_runs rows — one per execute
    invocation — with NO timestamp inference. Every registered row is
    returned (current and superseded) under its real run with run_id /
    started_at / finished_at / status / outcome / artifact_count /
    current_count / artifacts. Historical rows keep their metadata even
    when the physical file is gone (``exists: false``). storage_path is
    NEVER included.

    Legacy rows written before Execution Runs existed keep their original
    provenance grouping and are marked ``is_legacy_run: true`` — no run row
    is ever fabricated for them. Deterministic: runs ordered by start,
    artifacts by (created_at, id).
    """
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    from sqlalchemy import select

    from app.models.task_artifact import TaskArtifact

    all_rows = list(
        (
            await db.execute(
                select(TaskArtifact)
                .where(TaskArtifact.task_id == task_id)
                .order_by(TaskArtifact.created_at.asc(), TaskArtifact.id.asc())
            )
        ).scalars().all()
    )
    if not all_rows:
        return {"task_id": task_id, "runs": []}

    runs_db = list(
        (
            await db.execute(
                select(ExecutionRun)
                .where(ExecutionRun.task_id == task_id)
                .order_by(ExecutionRun.started_at.asc(), ExecutionRun.created_at.asc())
            )
        ).scalars().all()
    )
    runs_by_id = {r.id: r for r in runs_db}

    def _artifact_dict(r) -> dict:
        resolved = _os.path.realpath(r.storage_path)
        contained = resolved.startswith(_os.path.realpath(TASK_WORKSPACE) + _os.sep)
        return {
            "artifact_id": r.id,
            "filename": r.filename,
            "size": r.size_bytes,
            "content_type": r.content_type,
            "hash": r.sha256,
            "step_id": r.step_id,
            "step_title": r.step_title,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "run_id": r.run_id,
            "is_current": r.is_current,
            "superseded_at": r.superseded_at.isoformat() if r.superseded_at else None,
            "exists": contained and _os.path.isfile(resolved),
        }

    def _run_dict(rid: str, run, rows) -> dict:
        arts = [_artifact_dict(r) for r in rows]
        if run is not None:
            # First-class run: fields come ONLY from the durable row.
            started = run.started_at.isoformat() if run.started_at else None
            finished = run.finished_at.isoformat() if run.finished_at else None
            status, outcome, is_legacy = run.status, run.outcome, False
        else:
            # Legacy/unscoped grouping (pre-Execution-Runs rows): honest
            # provenance only — earliest artifact timestamp is NOT a
            # fabricated run start, and no status/outcome is invented.
            started = rows[0].created_at.isoformat() if rows and rows[0].created_at else None
            finished, status, outcome, is_legacy = None, None, None, True
        return {
            "run_id": rid,
            "started_at": started,
            "finished_at": finished,
            "status": status,
            "outcome": outcome,
            "is_legacy_run": is_legacy,
            "last_artifact_at": rows[-1].created_at.isoformat() if rows and rows[-1].created_at else None,
            "artifact_count": len(arts),
            "current_count": sum(1 for a in arts if a["is_current"]),
            "artifacts": arts,
        }

    # Group rows: first-class runs by their real run_id; anything else
    # (old hex ids, "backfill") keeps its stored provenance grouping as
    # legacy/unscoped — never folded into a fabricated run row.
    rows_by_run: dict[str, list] = {}
    legacy_by_id: dict[str, list] = {}
    for r in all_rows:
        if r.run_id and r.run_id in runs_by_id:
            rows_by_run.setdefault(r.run_id, []).append(r)
        else:
            legacy_by_id.setdefault(r.run_id or "unscoped", []).append(r)

    out_runs: list[dict] = []
    for run in runs_db:
        # A real execution with no registered artifacts is still a real
        # execution — reported with an empty artifact list.
        out_runs.append(_run_dict(run.id, run, rows_by_run.get(run.id, [])))
    for rid, rows in legacy_by_id.items():
        out_runs.append(_run_dict(rid, None, rows))
    out_runs.sort(key=lambda d: (d["started_at"] or "", d["run_id"]))
    return {"task_id": task_id, "runs": out_runs}


@app.get("/api/v1/tasks/{task_id}/artifacts/rows/{artifact_id}")
async def download_task_artifact_by_id(
    current_user: Annotated[dict, Depends(get_current_user_dep)],
    task_id: str,
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Download one artifact by its stable registry ID (historical-safe).

    Ownership via the task; the row must belong to THIS task (a foreign
    artifact_id matches nothing). The path is the one persisted at
    registration — re-validated for workspace containment before bytes are
    served. A physically deleted artifact is an honest 404; metadata-only
    history never fabricates contents.
    """
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    from sqlalchemy import select

    from app.models.task_artifact import TaskArtifact

    row = (
        await db.execute(
            select(TaskArtifact).where(
                TaskArtifact.id == artifact_id,
                TaskArtifact.task_id == task_id,
            )
        )
    ).scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    resolved = _os.path.realpath(row.storage_path)
    workspace_root = _os.path.realpath(TASK_WORKSPACE)
    if not resolved.startswith(workspace_root + _os.sep) or not _os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="Artifact file is missing from the workspace")
    # Superseded rows record bytes that were replaced at this path. Serving
    # the file's CURRENT bytes under that row's identity would fabricate
    # history — the row's hash must still match what is on disk.
    if not row.is_current:
        import hashlib as _hashlib

        with open(resolved, "rb") as fh:
            digest = _hashlib.sha256(fh.read()).hexdigest()
        if digest != row.sha256:
            raise HTTPException(
                status_code=404,
                detail="Historical artifact bytes were replaced by a later run — metadata only",
            )
    return FileResponse(
        resolved,
        media_type=row.content_type or "application/octet-stream",
        filename=row.filename,
    )
    """Current registry rows for a task, deterministic order (created, id)."""
    from sqlalchemy import select

    from app.models.task_artifact import TaskArtifact

    q = (
        select(TaskArtifact)
        .where(TaskArtifact.task_id == task_id, TaskArtifact.is_current == True)  # noqa: E712
        .order_by(TaskArtifact.created_at.asc(), TaskArtifact.id.asc())
    )
    return list(db.execute(q).scalars().all())


async def _registry_rows_for_task_async(db, task_id: str):
    from sqlalchemy import select

    from app.models.task_artifact import TaskArtifact

    q = (
        select(TaskArtifact)
        .where(TaskArtifact.task_id == task_id, TaskArtifact.is_current == True)  # noqa: E712
        .order_by(TaskArtifact.created_at.asc(), TaskArtifact.id.asc())
    )
    return list((await db.execute(q)).scalars().all())


async def _registry_rows_for_run_async(db, task_id: str, run_id: str):
    """ALL registry rows (current + superseded) for ONE run of a task.

    Scoped by BOTH task_id and run_id: a run_id from another task matches
    nothing here, so a foreign run can never leak another task's rows.
    Deterministic order (created, id).
    """
    from sqlalchemy import select

    from app.models.task_artifact import TaskArtifact

    q = (
        select(TaskArtifact)
        .where(TaskArtifact.task_id == task_id, TaskArtifact.run_id == run_id)
        .order_by(TaskArtifact.created_at.asc(), TaskArtifact.id.asc())
    )
    return list((await db.execute(q)).scalars().all())


async def _task_artifacts_internal(db, task) -> list[dict]:
    """Internal artifact dicts for a task — REGISTRY PRIMARY, legacy fallback.

    Shape matches _collect_task_artifacts plus registry fields; "path" is
    internal and must never reach a client response. Registry rows point at
    paths that were validated+hashed at registration; containment is
    re-checked here anyway (defense in depth).
    """
    rows = await _registry_rows_for_task_async(db, task.id)
    arts: list[dict] = []
    if rows:
        workspace_root = _os.path.realpath(TASK_WORKSPACE)
        for i, r in enumerate(rows):
            resolved = _os.path.realpath(r.storage_path)
            contained = resolved.startswith(workspace_root + _os.sep)
            exists = contained and _os.path.isfile(resolved)
            arts.append(
                {
                    "index": i,
                    "artifact_id": r.id,
                    "step_id": r.step_id,
                    "step_title": r.step_title,
                    "filename": r.filename,
                    "kind": "registry",
                    "exists": exists,
                    "size": _os.path.getsize(resolved) if exists else None,
                    "path": resolved,
                    "hash": r.sha256,
                    "content_type": r.content_type,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )
        return arts
    legacy = _collect_task_artifacts(task)
    for a in legacy:
        a.setdefault("artifact_id", None)
        a.setdefault("hash", None)
        a.setdefault("content_type", None)
        a.setdefault("created_at", None)
    return legacy


def _registry_artifact_dict(row, index: int) -> dict:
    """Internal dict for one registry row — same shape as _task_artifacts_internal.

    Containment is re-checked here (defense in depth); "path" is internal
    and must never reach a client response.
    """
    resolved = _os.path.realpath(row.storage_path)
    contained = resolved.startswith(_os.path.realpath(TASK_WORKSPACE) + _os.sep)
    exists = contained and _os.path.isfile(resolved)
    return {
        "index": index,
        "artifact_id": row.id,
        "step_id": row.step_id,
        "step_title": row.step_title,
        "filename": row.filename,
        "kind": "registry",
        "exists": exists,
        "size": _os.path.getsize(resolved) if exists else None,
        "path": resolved,
        "hash": row.sha256,
        "content_type": row.content_type,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _artifact_public(row) -> dict:
    """Safe public shape of a registry row — NEVER exposes storage_path."""
    resolved = _os.path.realpath(row.storage_path)
    return {
        "artifact_id": row.id,
        "filename": row.filename,
        "size": row.size_bytes,
        "content_type": row.content_type,
        "hash": row.sha256,
        "step_id": row.step_id,
        "step_title": row.step_title,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        # Compatibility fields consumed by the existing UI/tests:
        "index": None,  # filled by callers
        "kind": "registry",
        "exists": _os.path.isfile(resolved),
    }


@app.get("/api/v1/tasks/{task_id}/artifacts/{index}")
async def download_task_artifact(
    current_user: Annotated[dict, Depends(get_current_user_dep)],
    task_id: str,
    index: int,
    db: AsyncSession = Depends(get_db),
):
    """Download one artifact by index — real bytes, ownership enforced.

    Primary source: the registry (paths were persisted only after the file
    really existed and was containment-validated). Legacy fallback: step
    results. Either way the path is re-validated before bytes are served.
    """
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = await _registry_rows_for_task_async(db, task_id)
    if rows:
        if index < 0 or index >= len(rows):
            raise HTTPException(status_code=404, detail="Artifact not found")
        resolved = _os.path.realpath(rows[index].storage_path)
        workspace_root = _os.path.realpath(TASK_WORKSPACE)
        if not resolved.startswith(workspace_root + _os.sep) or not _os.path.isfile(resolved):
            raise HTTPException(status_code=404, detail="Artifact file is missing from the workspace")
        return FileResponse(
            resolved,
            media_type=rows[index].content_type or "application/octet-stream",
            filename=rows[index].filename,
        )
    # Legacy fallback: derive from step results (pre-registry tasks).
    arts = _collect_task_artifacts(task)
    if index < 0 or index >= len(arts):
        raise HTTPException(status_code=404, detail="Artifact not found")
    a = arts[index]
    if not a["exists"]:
        raise HTTPException(status_code=404, detail="Artifact file is missing from the workspace")
    return FileResponse(
        a["path"],
        media_type="application/octet-stream",
        filename=a["filename"],
    )


def _bundle_zip(arts: list[dict]) -> bytes:
    """Build the ZIP from revalidated artifacts, honestly.

    Filenames are flattened (basename only — no directory components can
    survive, so path traversal inside the archive is impossible) and
    deduplicated. A MISSING artifact is NOT silently omitted: a
    `MISSING.txt` entry records exactly which files were absent. Empty
    artifacts are included as empty files (real bytes of length zero).
    """
    import zipfile

    buf = io.BytesIO()
    used: set[str] = set()
    missing: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for a in arts:
            # Basename-only + dedup: 'x.py', 'generated/y.py' → 'y.py',
            # a second 'y.py' → 'y_2.py'. No '/' or '..' can survive.
            name = _os.path.basename(a["filename"].replace("\\", "/")) or "artifact"
            candidate, k = name, 2
            while candidate in used:
                stem, ext = _os.path.splitext(name)
                candidate = f"{stem}_{k}{ext}"
                k += 1
            used.add(candidate)
            if not a["exists"]:
                missing.append(name)
                continue
            try:
                with open(a["path"], "rb") as f:
                    zf.writestr(candidate, f.read())
            except OSError:
                missing.append(name)
        if missing:
            zf.writestr(
                "MISSING.txt",
                "The following artifacts were recorded for this task but "
                "could not be read from the workspace:\n"
                + "\n".join(f"- {m}" for m in missing)
                + "\n",
            )
    return buf.getvalue()


def _enforce_bundle_limits(arts: list[dict]) -> None:
    """Reject a bundle request that exceeds the configured ZIP limits.

    Checked BEFORE the archive is built (count) and before it is returned
    (uncompressed byte total), so a single request can never materialize
    an arbitrarily large ZIP. Raises 413 with a stable, path-free message.
    """
    if len(arts) > settings.max_zip_files:
        raise HTTPException(
            status_code=413,
            detail="Artifact bundle exceeds the configured MAX_ZIP_FILES limit",
        )
    total = 0
    for a in arts:
        if a.get("exists") and a.get("size"):
            total += int(a["size"])
    if total > settings.max_zip_size_bytes:
        raise HTTPException(
            status_code=413,
            detail="Artifact bundle exceeds the configured MAX_ZIP_SIZE_BYTES limit",
        )


@app.get("/api/v1/tasks/{task_id}/artifacts/bundle/download")
async def download_task_artifact_bundle(
    current_user: Annotated[dict, Depends(get_current_user_dep)],
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Download ALL of a task's artifacts as one ZIP (owner-scoped).

    Same derivation and revalidation as the individual endpoint (paths are
    re-derived from persisted step results and re-checked for workspace
    containment; nothing client-supplied is trusted). Honest behavior:
    - task unknown / not yours → 404
    - task with no artifacts → 404 ("no artifacts")
    - artifacts recorded but unreadable → ZIP still returned, with a
      MISSING.txt entry naming them (never silently omitted)
    """
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    arts = await _task_artifacts_internal(db, task)
    if not arts:
        raise HTTPException(status_code=404, detail="Task has no artifacts")
    _enforce_bundle_limits(arts)
    data = _bundle_zip(arts)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{task_id}-artifacts.zip"'
        },
    )


def _sanitize_zip_component(name: str) -> str:
    """Flatten one path component for archive use.

    Basename only (no separators survive), traversal fragments stripped,
    control characters removed, capped length, never empty.
    """
    comp = _os.path.basename(name.replace("\\", "/")).strip()
    for frag in ("..",):
        comp = comp.replace(frag, "")
    comp = "".join(c for c in comp if c.isalnum() or c in " ._-()[]") or "artifact"
    return comp[:80].strip() or "artifact"


def _project_bundle(
    tasks: list,
    project_name: str = "",
    project_id: str = "",
    rows_by_task: dict | None = None,
) -> bytes:
    """Build the project ZIP from REGISTRY rows (legacy collector fallback).

    Entries are namespaced per task (`task_<short-id>/<filename>`) so task
    context is preserved and cross-task collisions are impossible;    within a
    task the per-task dedup rules apply. Ordering is deterministic: tasks by
    created_at/id, artifacts by their persisted order. Identical physical
    content within one task is written once with a `_same-hash` note rather
    than duplicated (cross-task copies are intentional task context).
    MISSING artifacts are recorded in the manifest (never
    silently omitted); no server paths, no credentials — the manifest
    reflects persisted DB state only.
    """
    import zipfile
    from datetime import datetime, timezone

    buf = io.BytesIO()
    manifest_lines = [
        "RAIVISOX AI — Project Artifact Export",
        f"project_id: {project_id}",
        f"project_name: {project_name}",
        f"Exported at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    total_artifacts = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for task in tasks:
            tid = task.id
            short = tid.replace("-", "")[:8]
            dir_comp = _sanitize_zip_component(f"task_{short}")
            # Content-dedup is scoped WITHIN one task: per-task namespaced
            # copies are the export's task context, so identical bytes from
            # two tasks still land under each task's directory. Within a
            # task, the registry never records duplicate paths, so this
            # only guards degenerate rows.
            written_contents: dict[bytes, str] = {}
            rows = (rows_by_task or {}).get(tid) or []
            if rows:
                arts = [_registry_artifact_dict(r, i) for i, r in enumerate(rows)]
            else:
                arts = _collect_task_artifacts(task)
            for a in sorted(arts, key=lambda x: x["index"]):
                total_artifacts += 1
                step = a.get("step_title") or ""
                status_line = (
                    f"task_id: {tid}\n"
                    f"task_title: {task.title}\n"
                    f"task_status: {task.status}\n"
                    f"task_outcome: {getattr(task, 'outcome', None) or 'n/a'}\n"
                    f"artifact_id: {a.get('artifact_id') or 'n/a'}\n"
                    f"artifact: {a['filename']}\n"
                    f"size: {a['size'] if a['size'] is not None else 'unknown'} bytes\n"
                    f"hash: {a.get('hash') or 'n/a'}\n"
                    f"step: {step}\n"
                    f"created_at: {a.get('created_at') or 'n/a'}\n"
                    f"on_disk: {'yes' if a['exists'] else 'NO — missing from workspace'}"
                )
                if not a["exists"]:
                    manifest_lines.append(status_line)
                    manifest_lines.append("")
                    continue
                entry_dir = f"{dir_comp}"
                base = _sanitize_zip_component(a["filename"])
                candidate, k = f"{entry_dir}/{base}", 2
                existing = set(zf.namelist())
                while candidate in existing:
                    stem, ext = _os.path.splitext(base)
                    candidate = f"{entry_dir}/{stem}_{k}{ext}"
                    k += 1
                try:
                    with open(a["path"], "rb") as f:
                        content = f.read()
                    if a.get("hash") and content in written_contents:
                        # Identical bytes already in the archive — do not
                        # duplicate the same physical artifact.
                        manifest_lines.append(
                            status_line
                            + f"\nduplicate_of: {written_contents[content]} (same sha256 — written once)"
                        )
                    else:
                        zf.writestr(candidate, content)
                        written_contents[content] = candidate
                        manifest_lines.append(status_line)
                except OSError:
                    # Unreadable at export time — recorded honestly in the
                    # manifest, never silently omitted from the accounting.
                    manifest_lines.append(status_line + "\nread_error: file unreadable")
                manifest_lines.append("")
            if not arts:
                manifest_lines.append(
                    f"task_id: {tid}\n"
                    f"task_title: {task.title}\n"
                    f"task_status: {task.status}\n"
                    "artifacts: none recorded"
                )
                manifest_lines.append("")
        manifest_lines.append(f"tasks_exported: {len(tasks)}")
        manifest_lines.append(
            f"artifacts_recorded: {total_artifacts} (missing/unreadable are listed above)"
        )
        zf.writestr("MANIFEST.txt", "\n".join(manifest_lines) + "\n")
    return buf.getvalue()


@app.get("/api/v1/projects/{project_id}/artifacts/bundle/download")
async def download_project_artifact_bundle(
    current_user: Annotated[dict, Depends(get_current_user_dep)],
    project_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Download ALL artifacts of a project's tasks as one ZIP (owner-scoped).

    Same security model as the per-task bundle: the project must belong to
    the requesting user (identical 404 semantics as GET /projects/{id}),
    artifacts derive from persisted step results via the existing collector
    (realpath workspace containment re-validated, nothing client-supplied).
    A project with zero artifacts returns an honest 404 — same convention as
    the per-task bundle. No admin bypass (the project model is owner-scoped).
    """
    from sqlalchemy import select

    project = await task_service.get_project_by_id(db, project_id, current_user.id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    from sqlalchemy.orm import selectinload

    q = (
        select(Task)
        .options(selectinload(Task.steps), selectinload(Task.events))
        .where(Task.project_id == project_id, Task.user_id == current_user.id)
        .order_by(Task.created_at.asc(), Task.id.asc())
    )
    result = await db.execute(q)
    tasks = list(result.scalars().all())
    if not tasks:
        raise HTTPException(status_code=404, detail="Project has no tasks")
    rows_by_task: dict[str, list] = {}
    for t in tasks:
        rows_by_task[t.id] = await _registry_rows_for_task_async(db, t.id)
    if not any(rows_by_task.values()) and not any(
        _collect_task_artifacts(t) for t in tasks
    ):
        raise HTTPException(status_code=404, detail="Project has no artifacts")

    # Configured ZIP limits apply across ALL of the project's artifacts.
    _all_arts: list[dict] = []
    for t in tasks:
        _all_arts.extend(await _task_artifacts_internal(db, t))
    _enforce_bundle_limits(_all_arts)

    name_comp = _sanitize_zip_component(project.name or "project")
    data = _project_bundle(
        tasks,
        project_name=project.name or "",
        project_id=project_id,
        rows_by_task=rows_by_task,
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="project-{name_comp}-{project_id[:8]}-artifacts.zip"'
        },
    )


@app.get("/health")
async def health_check():
    """Health check endpoint (kept for compatibility)."""
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": settings.app_version,
    }


@app.get("/health/live")
async def health_live():
    """Liveness: the process is up and serving requests.

    Deliberately does NO dependency checks — an orchestrator restarts on
    non-200 here, and dependencies going down must not kill the process.
    """
    return {
        "status": "alive",
        "service": settings.app_name,
        "version": settings.app_version,
    }


async def _database_ready() -> bool:
    """True when a database round-trip succeeds."""
    try:
        async with async_session_factory() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — readiness must never raise
        return False


def _storage_ready() -> bool:
    """True when the task workspace exists or can be created."""
    try:
        _os.makedirs(TASK_WORKSPACE, exist_ok=True)
        return _os.path.isdir(TASK_WORKSPACE)
    except OSError:
        return False


async def _llm_readiness() -> dict:
    """Provider readiness WITHOUT exposing endpoints or keys.

    ``configured`` = an Ollama endpoint or provider API key is set.
    ``reachable``  = a cheap probe of a configured local endpoint
    (null when nothing is probed: no endpoint, or a hosted provider
    where key presence is the readiness signal).
    """
    configured = provider_configured(settings)
    if not configured:
        return {"configured": False, "reachable": None}
    base = _os.environ.get("RAIVISOX_LLM_BASE_URL", "").strip()
    if not base or not base.startswith(("http://", "https://")):
        # Hosted provider (or non-probeable scheme): key presence only.
        return {"configured": True, "reachable": None}
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(base.rstrip("/") + "/models")
        return {"configured": True, "reachable": resp.status_code < 500}
    except Exception:  # noqa: BLE001 — readiness must never raise
        return {"configured": True, "reachable": False}


@app.get("/health/ready")
async def health_ready():
    """Readiness: can serve real traffic right now.

    Gates on database + artifact storage (both required to serve any
    request honestly). Reports provider readiness as advisory — the API
    can serve auth/read-only traffic without an LLM. Response contains no
    secrets, URLs or filesystem paths.
    """
    db_ok = await _database_ready()
    store_ok = _storage_ready()
    llm = await _llm_readiness()
    ready = db_ok and store_ok
    body = {
        "status": "ready" if ready else "not_ready",
        "service": settings.app_name,
        "version": settings.app_version,
        "checks": {
            "database": "ok" if db_ok else "unavailable",
            "storage": "ok" if store_ok else "unavailable",
            "llm": llm,
        },
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "description": "From Intent to Verified Outcome",
        "docs": "/docs",
    }


import asyncio
import json


def _default_params(adapter_id: str, node_id: str, step, workspace: str = "", task_goal: str = "") -> dict:
    """Real, safe default parameters per adapter when none are supplied."""
    if adapter_id == "codegen":
        # Generate from the TASK goal (the code serves the whole task, not
        # just this step), with the step description as extra steering.
        return {"goal": f"{task_goal}. {step.description}".strip(", ")}
    if adapter_id == "finalize":
        # Document is injected per-run from real dependency outputs by the
        # worker builder; path/title are the safe defaults here.
        return {"path": f"deliverables/{node_id}.md", "title": "Task Deliverable"}
    if adapter_id == "fs_write":
        return {
            "path": f"outputs/{node_id}.txt",
            "content": f"Step: {step.title}\nTask artifact written by the fs_write adapter.\n",
        }
    if adapter_id == "fs_read":
        # Prefer the newest real artifact already in the workspace (a
        # previous fs_write step may have produced one); the step reads a
        # file that genuinely exists. Falls back to its own output path,
        # which fails honestly when nothing exists yet.
        read_path = f"outputs/{node_id}.txt"
        out_dir = _os.path.join(workspace, "outputs") if workspace else ""
        if out_dir and _os.path.isdir(out_dir):
            existing = [
                _os.path.join("outputs", f)
                for f in _os.listdir(out_dir)
                if f.endswith(".txt")
            ]
            if existing:
                read_path = max(
                    existing,
                    key=lambda p: _os.path.getmtime(_os.path.join(workspace, p)),
                )
        return {"path": read_path}
    if adapter_id == "shell_exec":
        return {"command": "echo hello from RAIVISOX pipeline"}
    if adapter_id == "python_exec":
        return {"code": "import platform; print('python_exec OK on', platform.system())"}
    if adapter_id == "text_transform":
        return {"op": "count", "text": f"{step.title}. {step.description}"}
    if adapter_id == "http_get":
        if "web_search" in step.required_capabilities:
            return {"op": "search", "query": f"{step.title}: {step.description}", "max_results": 3}
        return {"op": "read", "url": "https://example.com/", "max_chars": 5000}
    if adapter_id == "text_intel":
        # Capability-first op selection, then step_type fallback.
        caps = set(getattr(step, "required_capabilities", None) or [])
        if "document_generation" in caps:
            op = "generate"
        elif "planning" in caps:
            op = "plan"
        elif "error_analysis" in caps:
            op = "analyze_error"
        elif "intent_parsing" in caps:
            op = "analyze"
        else:
            op_map = {
                "understand": "analyze",
                "plan": "plan",
                "verify": "summarize",
                "debug": "analyze_error",
            }
            op = op_map.get(getattr(step, "step_type", "") or "", "analyze")
        return {"op": op, "text": f"{step.title}. {step.description}"}
    return {}


def _compose_deliverable_document(
    task_title: str,
    decomposed: list,
    exec_or_graph,  # ParallelExecutor (during/after execute) or TaskGraph
) -> str:
    """Build the deliverable document from REAL dependency outputs.

    The finalize adapter writes this document to a real file. Every line is
    derived from actual pipeline state (step results), never invented.
    Called at worker-build time with the graph as it exists then — for a
    deliver step that depends on earlier steps, those nodes may still be
    pending, so the per-step section reflects statuses at write time and is
    re-written on recovery retries as earlier steps complete.
    """
    graph = getattr(exec_or_graph, "graph", exec_or_graph)
    lines = [
        f"# {task_title}",
        "",
        "## Pipeline deliverable",
        "",
        f"- Steps in plan: {len(decomposed)}",
        "",
        "## Step results",
        "",
    ]
    for i, s in enumerate(decomposed):
        node_id = f"step-{i + 1}"
        node = graph.nodes.get(node_id)
        status = node.status.value if node else "pending"
        lines.append(f"### {i + 1}. {s.title} — {status}")
        if node and isinstance(node.result, dict):
            tool = node.result.get("tool")
            if tool:
                lines.append(f"- executed by: {tool}")
            out = node.result.get("output")
            if isinstance(out, dict):
                for key in ("summary", "document", "goal", "root_cause", "stdout"):
                    if isinstance(out.get(key), str) and out[key].strip():
                        lines.append(f"- {key}: {out[key].strip()[:400]}")
                        break
                if isinstance(out.get("results"), list) and out["results"]:
                    first = out["results"][0]
                    if isinstance(first, dict) and first.get("title"):
                        lines.append(f"- top finding: {first['title'][:200]}")
                if out.get("path"):
                    lines.append(f"- artifact: {_display_path(out['path'])}")
            elif isinstance(out, str) and out.strip():
                lines.append(f"- output: {out.strip()[:400]}")
        if node and node.error:
            lines.append(f"- error: {node.error[:300]}")
        lines.append("")
    lines.append("_Generated by the RAIVISOX execution pipeline from real step outputs._")
    return "\n".join(lines)


def _collect_task_artifacts(task) -> list[dict]:
    """Derive the artifact list from REAL persisted step results.

    An artifact is any file the pipeline actually wrote (recorded in a
    step's output as `written_to`). Only paths inside the task workspace
    tree are ever surfaced; client-supplied paths play no role — the
    download endpoint re-derives this list and addresses files by index.
    Entries carry an internal "path" key that the list endpoint strips
    before responding (server paths are never leaked to clients).
    """
    artifacts: list[dict] = []
    seen: set[str] = set()
    workspace_root = _os.path.realpath(TASK_WORKSPACE)
    for st in getattr(task, "steps", None) or []:
        res = getattr(st, "result", None)
        if not isinstance(res, dict):
            continue
        out = res.get("output")
        if not isinstance(out, dict):
            continue
        path = out.get("written_to")
        if not isinstance(path, str) or not path or path in seen:
            continue
        seen.add(path)
        resolved = _os.path.realpath(path)
        if not resolved.startswith(workspace_root + _os.sep):
            continue
        exists = _os.path.isfile(resolved)
        artifacts.append(
            {
                "index": len(artifacts),
                "step_id": st.id,
                "step_title": st.title,
                "filename": _os.path.basename(resolved),
                "kind": out.get("operation") or "file",
                "exists": exists,
                "size": _os.path.getsize(resolved) if exists else None,
                "path": resolved,  # internal — stripped before list responses
            }
        )
    return artifacts


# ---------------------------------------------------------------------------
# Artifact Registry — first-class persistence of files the pipeline wrote.
# ---------------------------------------------------------------------------


def _sha256_of_file(path: str, buf_size: int = 1 << 20) -> str:
    """Stable sha256 of the bytes actually on disk."""
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


_CONTENT_TYPES = {
    ".py": "text/x-python",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
    ".html": "text/html",
    ".xml": "application/xml",
    ".yml": "text/yaml",
    ".yaml": "text/yaml",
    ".toml": "application/toml",
    ".log": "text/plain",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".sh": "text/x-shellscript",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
}


def _content_type_for(filename: str) -> str:
    import mimetypes

    return _CONTENT_TYPES.get(
        _os.path.splitext(filename)[1].lower(),
        mimetypes.guess_type(filename)[0] or "application/octet-stream",
    )


async def _register_task_artifact(
    task_id: str,
    *,
    path: str,
    node_id: str,
    step_db_id: Optional[str],
    step_title: str,
    run_id: Optional[str],
) -> "TaskArtifact":
    """Register one real artifact row AFTER the file exists on disk.

    Order of operations is the honesty contract: the file must already be
    successfully written and workspace-contained before anything is
    persisted. Uses its own DB session (the pipeline holds the request
    session); a failure here propagates to the caller, which keeps
    execution state intact and records the failure honestly — never
    fabricates an artifact row.
    """
    from datetime import datetime

    from sqlalchemy import select

    from app.models.task_artifact import TaskArtifact

    workspace_root = _os.path.realpath(TASK_WORKSPACE)
    resolved = _os.path.realpath(path)
    if not _os.path.isfile(resolved):
        raise FileNotFoundError(f"artifact file missing at registration: {path}")
    if not resolved.startswith(workspace_root + _os.sep):
        raise ValueError("artifact path escaped the task workspace; refusing to register")
    size = _os.path.getsize(resolved)
    if size > settings.max_artifact_size_bytes:
        # Configurable storage ceiling (MAX_ARTIFACT_SIZE_BYTES). Honest:
        # the registration fails loudly, nothing is fabricated or hidden.
        raise ValueError(
            "artifact exceeds the configured MAX_ARTIFACT_SIZE_BYTES limit; "
            "refusing to register"
        )
    digest = _sha256_of_file(resolved)

    async with async_session_factory() as session:
        # Per-task registry ceiling (MAX_ARTIFACTS_PER_TASK): counts ALL
        # rows including superseded history, so storage stays bounded.
        from sqlalchemy import func

        _existing = (
            await session.execute(
                select(func.count())
                .select_from(TaskArtifact)
                .where(TaskArtifact.task_id == task_id)
            )
        ).scalar() or 0
        if _existing >= settings.max_artifacts_per_task:
            raise ValueError(
                "task already has the configured MAX_ARTIFACTS_PER_TASK "
                "registered artifacts; refusing to register"
            )
        # Same-path history semantics: a later write to an identical path
        # (rerun / recovery regeneration) supersedes the previous CURRENT
        # row for THIS task — history preserved, nothing overwritten.
        #
        # Race safety: two parallel nodes completing on the same path could
        # both read "no current row" before either commits (SQLite has no
        # row locks), producing two current rows. Taking the write lock via
        # an UPDATE **before** the SELECT serializes same-path registrations
        # — the second transaction waits for the first to commit, then sees
        # the fresh current row and supersedes it normally.
        await session.execute(
            text(
                "UPDATE task_artifacts SET superseded_at = superseded_at "
                "WHERE task_id = :tid AND storage_path = :p AND is_current = "
                + ("TRUE" if database_pkg._is_postgres else "1")
            ),
            {"tid": task_id, "p": resolved},
        )
        q = (
            select(TaskArtifact)
            .where(
                TaskArtifact.task_id == task_id,
                TaskArtifact.storage_path == resolved,
                TaskArtifact.is_current == True,  # noqa: E712
            )
            .execution_options(synchronize_session=False)
        )
        prev = (await session.execute(q)).scalars().first()
        if prev:
            prev.is_current = False
            prev.superseded_at = datetime.utcnow()
            session.add(prev)
        row = TaskArtifact(
            task_id=task_id,
            step_id=step_db_id,
            node_id=node_id,
            step_title=step_title,
            run_id=run_id,
            filename=_os.path.basename(resolved),
            storage_path=resolved,
            size_bytes=size,
            content_type=_content_type_for(resolved),
            sha256=digest,
            is_current=True,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _backfill_task_artifacts() -> None:
    """Idempotent startup backfill: registry rows for pre-registry tasks.

    Tasks executed before the registry existed have real artifacts visible
    only through the legacy step-result derivation. For every task with NO
    registry rows at all, each distinct `written_to` path in its persisted
    step results becomes a row — but ONLY if the file still exists inside
    the workspace (never fabricate) — sized and hashed exactly like live
    registration, with run_id="backfill" as provenance.

    Idempotency: a task with ANY registry rows (current or superseded) is
    skipped, and already-recorded paths are skipped, so re-running adds
    nothing and duplicates nothing. Failures are recorded and non-fatal:
    backfill must never break startup.
    """
    from datetime import datetime

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models.task_artifact import TaskArtifact

    try:
        async with async_session_factory() as db:
            have_rows = {
                tid
                for (tid,) in (
                    await db.execute(select(TaskArtifact.task_id).distinct())
                ).all()
            }
            tasks = (
                (
                    await db.execute(
                        select(Task)
                        .options(selectinload(Task.steps))
                        .order_by(Task.created_at.asc(), Task.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            workspace_root = _os.path.realpath(TASK_WORKSPACE)
            backfilled = 0
            for task in tasks:
                if task.id in have_rows:
                    continue  # already registered (fully or partially) — skip
                task_had_row = False
                seen: set[str] = set()
                for st in task.steps or []:
                    res = getattr(st, "result", None)
                    out = res.get("output") if isinstance(res, dict) else None
                    path = out.get("written_to") if isinstance(out, dict) else None
                    if not isinstance(path, str) or not path or path in seen:
                        continue
                    seen.add(path)
                    resolved = _os.path.realpath(path)
                    if not resolved.startswith(workspace_root + _os.sep):
                        continue
                    if not _os.path.isfile(resolved):
                        continue  # never fabricate a row for a vanished file
                    prev = (
                        await db.execute(
                            select(TaskArtifact).where(
                                TaskArtifact.task_id == task.id,
                                TaskArtifact.storage_path == resolved,
                            )
                        )
                    ).scalars().first()
                    if prev is not None:
                        task_had_row = True
                        continue  # duplicate prevention
                    db.add(
                        TaskArtifact(
                            task_id=task.id,
                            step_id=st.id,
                            step_title=st.title,
                            run_id="backfill",
                            filename=_os.path.basename(resolved),
                            storage_path=resolved,
                            size_bytes=_os.path.getsize(resolved),
                            content_type=_content_type_for(resolved),
                            sha256=_sha256_of_file(resolved),
                            is_current=True,
                        )
                    )
                    backfilled += 1
                    task_had_row = True
                if task_had_row:
                    task.updated_at = datetime.utcnow()
            if backfilled:
                await db.commit()
                logger.info("artifact registry backfill registered=%d", backfilled)
    except Exception as exc:  # noqa: BLE001 — never break startup
        logger.warning("artifact registry backfill skipped (non-fatal): %s", exc)


# Graph-node status → persisted TaskStep status. Only transitions the
# executor actually reports are mapped; "skipped" maps to CANCELLED to
# match the pass-boundary mirror's semantics.
_TRANSITION_TO_STEP_STATUS = {
    "started": "executing",
    "completed": "completed",
    "failed": "failed",
    "skipped": "cancelled",
}


async def _emit_step_event(
    task_id: str,
    event_type: str,
    payload: dict,
    step_row_id: str | None = None,
    execution_run_id: str | None = None,
) -> None:
    """Persist a real pipeline transition on its own DB session.

    When step_row_id is given, the matching TaskStep row is updated IN THE
    SAME transaction (fine-grained persistence): the event and the row can
    never disagree, and live pollers observe transitions as they happen.
    Runs concurrently with the pipeline (which holds the request session).
    A failure here must NEVER break execution — progress observability is
    not control flow. ``execution_run_id`` associates the new event with its
    first-class ExecutionRun (None only for legacy/sweep callers).
    """
    # One central choke point: no event payload ever carries an internal
    # filesystem path, whichever producer emitted it.
    if isinstance(payload.get("error"), str):
        payload["error"] = _scrub_internal_paths(payload["error"])
    try:
        async with async_session_factory() as session:
            await task_service.create_task_event(
                session,
                task_id=task_id,
                event_type=event_type,
                payload=payload,
                execution_run_id=execution_run_id,
            )
            if step_row_id:
                new_status = _TRANSITION_TO_STEP_STATUS.get(payload.get("transition") or "")
                if new_status:
                    from sqlalchemy import select

                    from app.models.task_step import TaskStep

                    row = (
                        await session.execute(
                            select(TaskStep).where(TaskStep.id == step_row_id)
                        )
                    ).scalars().first()
                    if row is not None:
                        row.status = new_status
                        if payload.get("error"):
                            row.error = {"message": payload["error"]}
                        result_out = payload.get("result")
                        if isinstance(result_out, dict):
                            row.result = result_out
            await session.commit()
    except Exception as e:  # pragma: no cover - observability only
        logger.error("step-event persist failed event_type=%s task_id=%s error=%s", event_type, task_id, e)


def _build_worker(
    node_id: str,
    step,  # decomposer TaskStep
    registry: AdapterRegistry,
    actor_id: str,
    task_id: str,
    workspace: str,
    task_title: str = "",
    decomposed: list | None = None,
    graph=None,  # TaskGraph — lets delivery steps read dependency outputs
    step_params: dict | None = None,
    fail_spec: dict | None = None,
):
    """Build the async worker for one graph node.

    Resolution order:
      1. A real adapter matching one of the step's required capabilities —
         the real tool is executed through the kernel-gated registry.
      2. A step that declares NO capabilities resolves to the default
         "finalize" adapter, which writes the task deliverable (composed
         from real dependency outputs) to a real workspace file.
      3. Capabilities that exist but map to no real adapter — the step
         stays SIMULATED and is reported with mode "simulated" (never
         claimed as real).
    If fail_spec is given (test hook), the worker fails its first
    `fail_times` invocations, then delegates — so the recovery retry loop
    re-executes the actual worker.
    """
    adapter_id = registry.select(step.required_capabilities)
    graph_ref = graph

    async def _run() -> dict:
        nonlocal adapter_id
        # Retry path: a step that was simulated on the first pass may
        # resolve to the default adapter once the graph exists.
        if not adapter_id:
            adapter_id = registry.select(step.required_capabilities)
        if adapter_id:
            params = (step_params or {}).get(node_id) or _default_params(
                adapter_id, node_id, step, workspace, task_goal=task_title
            )
            if adapter_id == "finalize" and graph_ref is not None and "document" not in params:
                # Inject the deliverable composed from real dependency
                # outputs — never overwriting caller-supplied step_params.
                params = {
                    **params,
                    "document": _compose_deliverable_document(
                        task_title or step.title, decomposed or [step], graph_ref
                    ),
                    "title": params.get("title") or task_title or step.title,
                }
            result = await registry.run(
                adapter_id,
                params,
                AdapterContext(
                    actor_id=actor_id,
                    task_id=task_id,
                    step_id=node_id,
                    resource=f"task:{task_id}/step:{node_id}",
                    workspace=workspace,
                ),
            )
            if result.permission_denied:
                raise PermissionError(result.error or "permission denied")
            if not result.success:
                raise RuntimeError(result.error or f"{adapter_id} failed")
            return {
                "title": step.title,
                "step_type": step.step_type,
                "tool": adapter_id,
                "mode": "real",
                "status": "completed",
                "output": result.output,
            }
        await asyncio.sleep(0.05)  # simulated stand-in
        return {
            "title": step.title,
            "step_type": step.step_type,
            "capabilities": step.required_capabilities,
            "mode": "simulated",
            "status": "completed",
        }

    if fail_spec:
        calls: dict[str, int] = {}
        budget = fail_spec["fail_times"]

        async def _flaky(key=node_id, inner=_run):
            calls[key] = calls.get(key, 0) + 1
            if calls[key] <= budget:
                raise RuntimeError(
                    f"Injected transient failure for '{key}' (call {calls[key]}/{budget})"
                )
            return await inner()

        return _flaky
    return _run


def _reset_codegen_nodes(graph, decomposed) -> list[str]:
    """Re-arm ONLY code-generation nodes (plus the finalize delivery node).

    Used after a DoD-only failure: the artifact was produced but does not
    satisfy the goal, so the meaningful recovery is regeneration — not a
    whole-pipeline retry. Completed non-codegen nodes keep their results.
    Returns the list of re-armed node ids (for observation/events).
    """
    codegen_ids = {
        f"step-{i + 1}"
        for i, s in enumerate(decomposed)
        if "code_generation" in (s.required_capabilities or [])
    }
    rearmed: list[str] = []
    for node_id, node in graph.nodes.items():
        if node_id in codegen_ids or (
            node.status == ExecutionStatus.COMPLETED
            and isinstance(node.result, dict)
            and node.result.get("tool") == "finalize"
        ):
            node.status = ExecutionStatus.PENDING
            node.error = ""
            node.started_at = 0.0
            node.completed_at = 0.0
            # The executor's group builder seeds "completed" from
            # graph._completed — a node re-armed to PENDING must be removed
            # from it, or execute() will never schedule it again.
            for tracked in ("_completed", "_failed"):
                members = getattr(graph, tracked, None)
                if members is not None:
                    members.discard(node_id)
            rearmed.append(node_id)
    return rearmed


@app.post("/api/v1/tasks/{task_id}/execute")
async def execute_task(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[dict, Depends(get_current_user_dep)],
    payload: dict | None = Body(default=None),
):
    """Execute a task through the pipeline.

    Synchronous by default (request waits for the terminal outcome — used
    by the regression suite and deterministic tests). With
    `{"async_mode": true}` the request returns **202 Accepted** immediately,
    the pipeline runs in the background, and all state (executing →
    verifying → recovering → terminal) is persisted on the task row, where
    any number of polling clients converge on it. A duplicate execute
    while a run is in flight returns 202 with the current status instead
    of starting a second pipeline.
    """
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if isinstance(payload, dict) and (payload.get("async_mode") or payload.get("resume_mode")):
        # Duplicate-execute guard: an in-flight run is already driving this
        # task — never start a second pipeline for it.
        if task.status in (TaskStatus.EXECUTING.value, TaskStatus.RECOVERING.value, TaskStatus.VERIFYING.value):
            return JSONResponse(
                status_code=202,
                content={
                    "task_id": task_id,
                    "status": task.status,
                    "detail": "execution already in progress",
                },
            )
        # Atomic claim: the status check above is racy (two concurrent
        # executes can both observe a non-active status). A conditional
        # UPDATE makes exactly one of them win; the loser gets the same
        # honest 202 "already in progress" answer.
        claim = await db.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status.notin_(
                    [
                        TaskStatus.EXECUTING.value,
                        TaskStatus.RECOVERING.value,
                        TaskStatus.VERIFYING.value,
                    ]
                ),
            )
            .values(
                status=TaskStatus.EXECUTING.value,
                updated_at=datetime.utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if claim.rowcount == 0:
            return JSONResponse(
                status_code=202,
                content={
                    "task_id": task_id,
                    "status": TaskStatus.EXECUTING.value,
                    "detail": "execution already in progress",
                },
            )
        await task_service.update_task_status(db, task, TaskStatus.EXECUTING)
        _pipeline_task = asyncio.get_running_loop().create_task(
            _drive_pipeline(task_id, current_user.id, payload)
        )
        # Tracked so graceful shutdown can cancel and await it instead of
        # destroying it mid-write when the event loop closes.
        _background_pipelines.add(_pipeline_task)
        _pipeline_task.add_done_callback(_background_pipelines.discard)
        return JSONResponse(
            status_code=202,
            content={
                "task_id": task_id,
                "status": TaskStatus.EXECUTING.value,
                "detail": (
                    "accepted — resuming with a fresh honest pipeline run"
                    if payload.get("resume_mode")
                    else "accepted — pipeline running in background"
                ),
            },
        )

    return await _execute_task_inner(db, task, task_id, payload)


async def _execute_task_inner(
    db: AsyncSession, task: Task, task_id: str, payload: dict | None
) -> dict:
    """Sync-mode pipeline call with an honest run-row crash backstop.

    If the pipeline raises after its run row was created, the row is closed
    as FAILED before the exception propagates — a synchronous crash must
    not leak a forever-``running`` execution_run either.
    """
    try:
        return await run_task_pipeline(db, task, task_id, payload)
    except Exception:
        await _close_open_execution_runs(
            task_id,
            status=ExecutionRunStatus.FAILED,
            outcome=TaskOutcome.FAILED.value,
            error="pipeline crashed during synchronous execute",
        )
        raise


async def _drive_pipeline(task_id: str, user_id: str, payload: dict | None) -> None:
    """Drive run_task_pipeline on its own DB session and persist failures.

    Runs as a detached background task. Every terminal state — including a
    crashed pipeline — is persisted, so any polling client converges to the
    truth (refresh-safe; visible across tabs and after server restart).
    """
    async with async_session_factory() as bg_db:
        try:
            task = await task_service.get_task_by_id(bg_db, task_id, user_id)
            if task is None:
                return
            await run_task_pipeline(bg_db, task, task_id, payload)
        except Exception as exc:  # noqa: BLE001 — persist crash as honest FAILED
            # User-visible crash text never carries internal fs paths.
            crash_msg = _scrub_internal_paths(f"pipeline crashed: {exc}")
            try:
                # First-Class Execution Runs: close the run this crash
                # orphaned — a run may never remain ``running``.
                await _close_open_execution_runs(
                    task_id,
                    status=ExecutionRunStatus.FAILED,
                    outcome=TaskOutcome.FAILED.value,
                    error=crash_msg,
                )
                task = await task_service.get_task_by_id(bg_db, task_id, user_id)
                if task is not None:
                    task.status = TaskStatus.FAILED.value
                    task.outcome = TaskOutcome.FAILED.value
                    task.error = {"message": crash_msg}
                    task.result = {
                        "message": crash_msg.replace("pipeline", "Pipeline", 1),
                        "outcome": TaskOutcome.FAILED.value,
                    }
                    await task_service.create_task_event(
                        bg_db,
                        task_id=task_id,
                        event_type=EventType.ERROR.value,
                        payload={
                            "error": _scrub_internal_paths(str(exc)),
                            "stage": "background_pipeline",
                        },
                    )
                    await bg_db.commit()
            except Exception:  # noqa: BLE001 — nothing more can be done
                pass


async def run_task_pipeline(
    db: AsyncSession,
    task: Task,
    task_id: str,
    payload: dict | None = None,
) -> dict:
    """Run the Phase 3–4 pipeline for an already-loaded task and persist
    every observable state (status, steps, events, outcome) as it advances.

    1. Decompose the task's intent into ordered, capability-tagged steps
    2. Persist the steps and build a dependency-aware TaskGraph
    3. Run the graph with ParallelExecutor (bounded parallelism, fail-fast)
    4. Verify the outcome with the VerificationEngine
    5. Verify goal satisfaction with executable Definition-of-Done checks
       derived from the task goal and run against the generated modules'
       ACTUAL solve() output (fresh subprocess, adapter result not trusted)
    6. On failure, consult the RecoveryEngine and ACT on its plan: an
       execution retry re-runs only failed/skipped steps; a DoD-only
       failure re-arms ONLY the code-generation nodes so the artifact is
       regenerated — completed nodes keep their results, and nodes that
       read the artifact (delivery) run again — up to MAX_RECOVERY_ATTEMPTS
       times. A task is finally successful only when every applicable DoD
       check passes.

    Optional JSON body:
      {"fail_step_index": 0, "fail_times": 1} makes the first step fail
        transiently (N times, then succeed) to exercise the
        verification → recovery → retry path end-to-end.
      {"step_params": {"step-3": {...}}} passes parameters to the adapter
        selected for that step (e.g. {"path": ..., "content": ...}).
      {"fail_dod_with": <value>} makes generated modules' DoD checks see an
        incorrect solve() result while execution itself stays clean —
        exercises the "executed fine, goal not met" path.
    Steps with no required capabilities resolve to the default "finalize"
    adapter, which writes the task deliverable (composed from real
    dependency outputs) to a real workspace file. Only steps whose
    capabilities map to no real adapter remain simulated.
    """
    # ---- 1. Decompose -------------------------------------------------
    intent = get_intent_engine().parse(task.goal or task.title)
    decomposed = get_task_decomposer().decompose(intent)
    if not decomposed:
        raise HTTPException(status_code=400, detail="Task produced no executable steps")

    payload = payload if isinstance(payload, dict) else {}
    fail_idx = payload.get("fail_step_index")
    fail_times = payload.get("fail_times", 1)
    step_params = payload.get("step_params")
    fail_dod_with = payload.get("fail_dod_with")
    fail_node_id = f"step-{fail_idx + 1}" if isinstance(fail_idx, int) else None
    # ---- 0. First-class execution run ---------------------------------
    # Exactly ONE durable row per execute invocation. Every execution path
    # (sync request session, async background session, restart resume) flows
    # through run_task_pipeline, so creating the row here is the single
    # choke point — the duplicate-execute guard above already prevents two
    # concurrent pipelines for one task. This UUID also becomes the artifact
    # registry's run_id and the task_events association id.
    execution_run = ExecutionRun(
        task_id=task_id,
        started_at=datetime.utcnow(),
        status=ExecutionRunStatus.RUNNING,
    )
    db.add(execution_run)
    await db.commit()
    await db.refresh(execution_run)
    run_id = execution_run.id

    # Identifies THIS execute invocation in artifact history (reruns keep
    # their own rows; see _register_task_artifact for the semantics).
    # First-Class Execution Runs: this is now the real run's UUID — the
    # old random hex[:16] ids remain only on legacy rows and are never
    # fabricated again.
    artifact_run_id = run_id

    # ---- 2. Persist steps + build the graph ---------------------------
    id_map = {s.id: f"step-{i + 1}" for i, s in enumerate(decomposed)}
    db_steps: dict[str, object] = {}
    worker_map: dict = {}

    async def _on_node_transition(node, transition: str) -> None:
        """Persist REAL executor node transitions as task events AND as
        fine-grained TaskStep row updates (same transaction per transition).

        Fired by ParallelExecutor at the actual state changes — this only
        records what genuinely happened; no fabricated progress. The row
        update makes each step's persisted status match its events at
        every instant (pass-boundary mirroring in _persist_step_outcomes
        remains as the consistent final sync).
        """
        etype = {
            "started": EventType.STEP_STARTED.value,
            "completed": EventType.STEP_COMPLETED.value,
            "failed": EventType.STEP_FAILED.value,
            "skipped": EventType.STEP_SKIPPED.value,
        }[transition]
        payload = {
            "step_id": node.id,
            "title": node.title,
            "status": node.status.value,
            "transition": transition,
        }
        if node.error:
            payload["error"] = node.error
        if node.completed_at and node.started_at:
            payload["duration"] = round(node.completed_at - node.started_at, 4)
        if transition == "completed" and isinstance(node.result, dict):
            # Real per-step output (e.g. the artifact path) lands on the
            # row the moment the node completes — not only at pass end.
            payload["result"] = node.result
            # Artifact Registry: register files this step REALLY wrote.
            # The file already exists (the adapter wrote it before the node
            # could complete); registration validates containment, sizes
            # and hashes the bytes on disk, then persists the row. A
            # registration failure is recorded as an event and never
            # corrupts execution state or fabricates an artifact.
            out = node.result.get("output")
            if isinstance(out, dict):
                reg_path = out.get("written_to") or out.get("path")
                if isinstance(reg_path, str) and reg_path:
                    db_step_reg = db_steps.get(node.id)
                    try:
                        await _register_task_artifact(
                            task_id,
                            path=reg_path,
                            node_id=node.id,
                            step_db_id=getattr(db_step_reg, "id", None),
                            step_title=node.title,
                            run_id=artifact_run_id,
                        )
                    except Exception as exc:  # honest, non-fatal
                        await _emit_step_event(
                            task_id,
                            EventType.ARTIFACT_REGISTRATION_FAILED.value,
                            {
                                "step_id": node.id,
                                "title": node.title,
                                "error": _scrub_internal_paths(str(exc))[:300],
                            },
                            execution_run_id=run_id,
                        )
        db_step = db_steps.get(node.id)
        await _emit_step_event(
            task_id,
            etype,
            payload,
            step_row_id=getattr(db_step, "id", None),
            execution_run_id=run_id,
        )

    executor = ParallelExecutor(max_concurrent=4, on_node_transition=_on_node_transition)
    observations = ObservationEngine()

    for i, s in enumerate(decomposed):
        db_step = await task_service.create_task_step(
            db,
            task,
            TaskStepCreate(
                title=s.title,
                description=s.description,
                order=s.order if isinstance(s.order, int) else i,
                dependencies=",".join(s.dependencies) if s.dependencies else None,
            ),
        )
        db_steps[f"step-{i + 1}"] = db_step

        fn = _build_worker(
            node_id=f"step-{i + 1}",
            step=s,
            registry=adapter_registry,
            actor_id=PIPELINE_ACTOR,
            task_id=task_id,
            workspace=TASK_WORKSPACE,
            task_title=task.goal or task.title or "",
            decomposed=decomposed,
            graph=executor.graph,
            step_params=step_params,
            fail_spec=(
                {"fail_times": fail_times} if fail_node_id == f"step-{i + 1}" else None
            ),
        )
        worker_map[f"step-{i + 1}"] = fn
        executor.add_step(
            id=f"step-{i + 1}",
            title=s.title,
            fn=fn,
            depends_on=[id_map[d] for d in s.dependencies if d in id_map],
        )

    MAX_RECOVERY_ATTEMPTS = 3

    async def _persist_step_outcomes(exec: ParallelExecutor) -> None:
        """Mirror graph node states into the persisted TaskStep rows."""
        for i in range(len(decomposed)):
            node = exec.graph.nodes.get(f"step-{i + 1}")
            db_step = db_steps.get(f"step-{i + 1}")
            if not node or not db_step:
                continue
            observations.record(node.id, f"step_{node.status.value}", {"title": node.title})
            if node.status == ExecutionStatus.COMPLETED:
                new_status = StepStatus.COMPLETED.value
            elif node.status == ExecutionStatus.FAILED:
                new_status = StepStatus.FAILED.value
            elif node.status == ExecutionStatus.SKIPPED:
                new_status = StepStatus.CANCELLED.value
            else:
                new_status = node.status.value
            await task_service.update_task_step(
                db,
                db_step,
                TaskStepUpdate(
                    status=new_status,
                    result=node.result if isinstance(node.result, dict) else None,
                    error={
                        "message": _scrub_internal_paths(node.error)
                    } if node.error else None,
                ),
            )

    def _verify(exec: ParallelExecutor):
        """Run verification checks over the current graph state."""
        node_status = {
            f"step-{i + 1}": exec.graph.nodes[f"step-{i + 1}"].status.value
            for i in range(len(decomposed))
        }
        expected_titles = [s.title for s in decomposed]

        def check_all_completed(outcome, criteria):
            completed = [st["title"] for st in outcome["steps"] if st["status"] == "completed"]
            missing = [t for t in expected_titles if t not in completed]
            return {"passed": not missing, "missing": missing}

        def check_no_failed(outcome, criteria):
            bad = [st["title"] for st in outcome["steps"] if st["status"] == "failed"]
            return {"passed": not bad, "failed_steps": bad}

        verifier = VerificationEngine()
        verifier.register_check("all_steps_completed", check_all_completed)
        verifier.register_check("no_failed_steps", check_no_failed)

        outcome = {
            "steps": [
                {"id": nid, "title": s.title, "status": node_status[nid]}
                for nid, s in zip(node_status.keys(), decomposed)
            ],
        }
        return outcome, verifier.verify(task_id, outcome)

    async def _reset_for_retry(exec: ParallelExecutor) -> None:
        """Re-arm failed/skipped nodes so the next execute() re-runs them.

        Completed nodes keep their status/result — the executor only picks
        up PENDING nodes, so a retry touches exactly the failed branch.
        """
        for node in exec.graph.nodes.values():
            if node.status in (ExecutionStatus.FAILED, ExecutionStatus.SKIPPED):
                node.status = ExecutionStatus.PENDING
                node.error = ""
                node.started_at = 0.0
                node.completed_at = 0.0

    async def _reset_codegen_for_dod(exec: ParallelExecutor) -> list[str]:
        """Re-arm ONLY the code-generation nodes after a DoD failure."""
        return _reset_codegen_nodes(exec.graph, decomposed)

    # ---- 3–6. Execute → verify → DoD → recover loop --------------------
    verification = None
    dod_summary: dict | None = None
    recovery = None
    recovery_history: list = []
    attempts = 0

    while True:
        attempts += 1
        await executor.execute()
        await _persist_step_outcomes(executor)

        await _emit_step_event(
            task_id,
            EventType.VERIFICATION.value,
            {"phase": "started", "attempt": attempts},
            execution_run_id=run_id,
        )
        outcome, verification = _verify(executor)
        await task_service.create_task_event(
            db,
            task_id=task_id,
            event_type=EventType.VERIFICATION.value,
            payload={
                "verdict": verification.verdict.value,
                "confidence": verification.confidence,
                "failed_checks": verification.failed_checks,
                "attempt": attempts,
            },
            execution_run_id=run_id,
        )

        if verification.verdict not in (Verdict.PASSED, Verdict.PARTIAL):
            if attempts > MAX_RECOVERY_ATTEMPTS:
                break
            # Ordinary execution failure — consult the RecoveryEngine.
            await task_service.update_task_status(
                db, task, TaskStatus.RECOVERING, execution_run_id=run_id
            )
            await _emit_step_event(
                task_id,
                EventType.RECOVERY_STARTED.value,
                {"attempt": attempts, "reason": "verification failed"},
                execution_run_id=run_id,
            )
            recovery = recovery_engine.recover(
                task_id=task_id,
                failure_info={
                    "error": "; ".join(verification.failed_checks) or "verification failed",
                    "retry_count": attempts - 1,
                    "failed_steps": [st["title"] for st in outcome["steps"] if st["status"] == "failed"],
                    "rollback_point": f"task:{task_id}",
                },
            )
            recovery_history.append(recovery)
            await task_service.create_task_event(
                db,
                task_id=task_id,
                event_type=EventType.RECOVERY.value,
                payload=recovery.to_dict(),
                execution_run_id=run_id,
            )
            if recovery.final_status == "pending_retry":
                observations.record("pipeline", "recovery_retry_scheduled", {"attempt": attempts})
                await _reset_for_retry(executor)
                await _emit_step_event(
                    task_id,
                    EventType.RECOVERY_COMPLETED.value,
                    {"attempt": attempts, "plan": recovery.to_dict()},
                    execution_run_id=run_id,
                )
                continue
            break

        # ---- Definition-of-Done stage (execution ≠ goal satisfaction) --
        # Collect real artifacts actually written by codegen steps.
        artifacts = [
            node.result["output"]["written_to"]
            for node in executor.graph.nodes.values()
            if isinstance(node.result, dict)
            and isinstance(node.result.get("output"), dict)
            and node.result["output"].get("operation") == "generate_module"
            and node.result["output"].get("written_to")
        ]
        dod_module_results = [
            await run_dod_checks_async(
                goal=task.goal or task.title or "",
                module_path=artifact,
                override_return=fail_dod_with,
                # Phase 0 containment: reject module dirs that resolve
                # outside the task workspace before spawning anything.
                workspace_root=TASK_WORKSPACE,
            )
            for artifact in artifacts
        ]
        dod_summary = summarize_dod(task.goal or task.title or "", dod_module_results)
        await task_service.create_task_event(
            db,
            task_id=task_id,
            event_type=EventType.VERIFICATION.value,
            payload={
                "verdict": "dod_"
                + ("satisfied" if dod_summary.get("satisfied") else "not_satisfied"),
                "definition_of_done": dod_summary,
                "attempt": attempts,
            },
            execution_run_id=run_id,
        )

        if dod_summary.get("applicable") and dod_summary.get("satisfied") is not True:
            # DoD-only failure: steps may all have completed, but the goal
            # is NOT met. Never report success — regenerate the artifacts.
            if attempts > MAX_RECOVERY_ATTEMPTS:
                break
            await _emit_step_event(
                task_id,
                EventType.RECOVERY_STARTED.value,
                {"attempt": attempts, "reason": "definition_of_done: goal not satisfied"},
                execution_run_id=run_id,
            )
            await task_service.update_task_status(
                db, task, TaskStatus.RECOVERING, execution_run_id=run_id
            )
            recovery = recovery_engine.recover(
                task_id=task_id,
                failure_info={
                    "error": "definition_of_done: goal not satisfied",
                    "retry_count": attempts - 1,
                    "dod_summary": dod_summary,
                    "rollback_point": f"task:{task_id}",
                },
            )
            recovery_history.append(recovery)
            await task_service.create_task_event(
                db,
                task_id=task_id,
                event_type=EventType.RECOVERY.value,
                payload=recovery.to_dict(),
                execution_run_id=run_id,
            )
            if recovery.final_status == "pending_retry":
                rearmed = await _reset_codegen_for_dod(executor)
                observations.record(
                    "pipeline",
                    "dod_recovery_regen_scheduled",
                    {"attempt": attempts, "rearmed_nodes": rearmed},
                )
                await _emit_step_event(
                    task_id,
                    EventType.RECOVERY_COMPLETED.value,
                    {"attempt": attempts, "rearmed_nodes": rearmed, "plan": recovery.to_dict()},
                    execution_run_id=run_id,
                )
                continue
            break

        # Every verification check passed AND the DoD stage is satisfied —
        # the goal is genuinely met; leave the recovery loop.
        break

    if verification.verdict == Verdict.PASSED and (
        dod_summary is None
        or not dod_summary.get("applicable")
        or dod_summary.get("satisfied") is True
    ):
        final_status = TaskStatus.COMPLETED
    elif verification.verdict == Verdict.PARTIAL and (
        dod_summary is None
        or not dod_summary.get("applicable")
        or dod_summary.get("satisfied") is not False
    ):
        final_status = TaskStatus.PARTIALLY_COMPLETED
    else:
        final_status = TaskStatus.FAILED

    # Set the terminal fields BEFORE the status commit so pollers never
    # observe "completed" with outcome/result still NULL (atomic terminal
    # state — the status flip is the last thing that becomes visible).
    _modes = [
        n.result.get("mode")
        for n in executor.graph.nodes.values()
        if isinstance(n.result, dict) and n.result.get("mode")
    ]
    simulated_count = _modes.count("simulated")
    outcome = compute_outcome(final_status, verification.verdict, dod_summary, simulated_count)
    task.outcome = outcome
    task.result = {
        "message": (
            f"Pipeline {verification.verdict.value} after {attempts} attempt(s): "
            f"{verification.confidence:.0%} of checks passed"
            + (
                f"; definition-of-done {'satisfied' if dod_summary.get('satisfied') else 'NOT satisfied'}"
                if dod_summary and dod_summary.get("applicable")
                else ""
            )
        ),
        "verification": {
            "verdict": verification.verdict.value,
            "confidence": verification.confidence,
            "failed_checks": verification.failed_checks,
        },
        "definition_of_done": dod_summary,
        "recovery": recovery.to_dict() if recovery else None,
        "recovery_attempts": len(recovery_history),
        "mode": (
            f"pipeline_v1 ({_modes.count('real')} real steps, "
            f"{simulated_count} simulated)"
        ),
        "outcome": outcome,
        "simulated_steps": simulated_count,
    }
    # First-Class Execution Runs: the terminal commit finishes the run row
    # at the SAME instant the task reaches its final status — finished_at,
    # status and outcome ride the terminal block, so the run and the task
    # can never disagree about how this execution ended.
    run_status = {
        TaskStatus.COMPLETED: ExecutionRunStatus.COMPLETED,
        TaskStatus.PARTIALLY_COMPLETED: ExecutionRunStatus.PARTIALLY_COMPLETED,
    }.get(final_status, ExecutionRunStatus.FAILED)
    await db.execute(
        update(ExecutionRun)
        .where(
            ExecutionRun.id == run_id,
            ExecutionRun.status == ExecutionRunStatus.RUNNING,
        )
        .values(
            finished_at=datetime.utcnow(),
            status=run_status,
            outcome=outcome,
        )
    )
    task = await task_service.update_task_status(db, task, final_status, execution_run_id=run_id)

    # Structured ops record: one line per terminal execution, parseable
    # by log pipelines (task_id / run id / status / outcome / duration).
    logger.info(
        "pipeline terminal task_id=%s execution_run_id=%s status=%s "
        "outcome=%s duration_s=%s",
        task.id,
        run_id,
        task.status,
        outcome,
        executor.graph.total_duration,
    )

    payload = {
        "task_id": task.id,
        "status": task.status,
        "outcome": outcome,
        "result": task.result,
        "verification": verification.to_dict(),
        "definition_of_done": dod_summary,
        "recovery": recovery.to_dict() if recovery else None,
        "recovery_attempts": len(recovery_history),
        "steps": [
            {
                "id": n.id,
                "title": n.title,
                "status": n.status.value,
                "duration": n.duration,
                "error": n.error or None,
            }
            for n in executor.graph.nodes.values()
        ],
        "observations": observations.summary(),
        "total_duration": executor.graph.total_duration,
    }
    # Response boundary: persisted results legitimately hold absolute
    # workspace paths, but a client must never receive the server's
    # filesystem layout — relativize before responding.
    from app.tasks.schemas import _relativize_workspace_paths

    return _relativize_workspace_paths(payload)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
    )
