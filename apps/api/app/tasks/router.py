"""Tasks and projects router."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.auth.service import get_current_user as auth_get_current_user
from app.tasks import service as task_service
from app.tasks.schemas import (
    TaskCreate,
    TaskUpdate,
    TaskResponse,
    TaskListResponse,
    TaskStepCreate,
    TaskStepUpdate,
    TaskStepResponse,
    TaskEventResponse,
    ProjectCreate,
    ProjectUpdate,
    ProjectResponse,
    ProjectListResponse,
    VerificationRequest,
    VerificationResultResponse,
)
from app.models.user import User

router = APIRouter(prefix="/api/v1", tags=["Tasks", "Projects"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=True)


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> User:
    """Get current authenticated user from Bearer token."""
    return await auth_get_current_user(db, token)


# Project endpoints

@router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    project_data: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProjectResponse:
    """Create a new project."""
    project = await task_service.create_project(db, current_user.id, project_data)
    return task_service.project_to_response(project)


@router.get("/projects", response_model=ProjectListResponse)
async def list_projects(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ProjectListResponse:
    """List projects for the current user."""
    projects, total = await task_service.get_projects(db, current_user.id, page, page_size)
    return ProjectListResponse(
        projects=[task_service.project_to_response(p) for p in projects],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProjectResponse:
    """Get a project by ID."""
    project = await task_service.get_project_by_id(db, project_id, current_user.id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    return task_service.project_to_response(project)


@router.put("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    project_data: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProjectResponse:
    """Update a project."""
    project = await task_service.get_project_by_id(db, project_id, current_user.id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    project = await task_service.update_project(db, project, project_data)
    return task_service.project_to_response(project)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a project."""
    project = await task_service.get_project_by_id(db, project_id, current_user.id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    await task_service.delete_project(db, project)


# Task endpoints

@router.post("/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    task_data: TaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskResponse:
    """Create a new task."""
    if task_data.project_id:
        project = await task_service.get_project_by_id(db, task_data.project_id, current_user.id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found",
            )

    task = await task_service.create_task(db, current_user.id, task_data)
    return task_service.task_to_response(task)


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status_filter: Optional[str] = Query(None, alias="status"),
    project_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> TaskListResponse:
    """List tasks for the current user."""
    tasks, total = await task_service.get_tasks(
        db, current_user.id, status_filter, project_id, page, page_size
    )
    return TaskListResponse(
        tasks=[task_service.task_to_response(t) for t in tasks],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskResponse:
    """Get a task by ID."""
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    return task_service.task_to_response(task)


@router.put("/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: str,
    task_data: TaskUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskResponse:
    """Update a task."""
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    task = await task_service.update_task(db, task, task_data)
    return task_service.task_to_response(task)


@router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskResponse:
    """Cancel a task."""
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    if task.status in ("completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel task with status {task.status}",
        )
    task = await task_service.cancel_task(db, task)
    return task_service.task_to_response(task)


@router.post("/tasks/{task_id}/resume", response_model=TaskResponse)
async def resume_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskResponse:
    """Resume a cancelled or waiting task."""
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    if task.status not in ("cancelled", "waiting_for_user"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot resume task with status {task.status}",
        )

    from app.models.task import TaskStatus
    task = await task_service.update_task_status(db, task, TaskStatus.EXECUTING)
    return task_service.task_to_response(task)


@router.get("/tasks/{task_id}/events", response_model=list[TaskEventResponse])
async def get_task_events(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
) -> list[TaskEventResponse]:
    """Get events for a task."""
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    events = await task_service.get_task_events(db, task_id, page, page_size)
    return [
        TaskEventResponse(
            id=e.id,
            task_id=e.task_id,
            step_id=e.step_id,
            event_type=e.event_type,
            payload=e.payload,
            created_at=e.created_at,
        )
        for e in events
    ]


# Task Step endpoints

@router.post("/tasks/{task_id}/steps", response_model=TaskStepResponse, status_code=status.HTTP_201_CREATED)
async def create_task_step(
    task_id: str,
    step_data: TaskStepCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskStepResponse:
    """Create a new task step."""
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    step = await task_service.create_task_step(db, task, step_data)
    return TaskStepResponse(
        id=step.id,
        task_id=step.task_id,
        parent_id=step.parent_id,
        title=step.title,
        description=step.description,
        status=step.status,
        order=step.order,
        dependencies=step.dependencies,
        model_used=step.model_used,
        tools_used=step.tools_used,
        timeout=step.timeout,
        result=step.result,
        error=step.error,
        created_at=step.created_at,
        updated_at=step.updated_at,
    )


@router.put("/tasks/{task_id}/steps/{step_id}", response_model=TaskStepResponse)
async def update_task_step(
    task_id: str,
    step_id: str,
    step_data: TaskStepUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskStepResponse:
    """Update a task step."""
    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    step = await task_service.get_task_step_by_id(db, step_id, task_id)
    if not step:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Step not found",
        )
    step = await task_service.update_task_step(db, step, step_data)
    return TaskStepResponse(
        id=step.id,
        task_id=step.task_id,
        parent_id=step.parent_id,
        title=step.title,
        description=step.description,
        status=step.status,
        order=step.order,
        dependencies=step.dependencies,
        model_used=step.model_used,
        tools_used=step.tools_used,
        timeout=step.timeout,
        result=step.result,
        error=step.error,
        created_at=step.created_at,
        updated_at=step.updated_at,
    )


# Verification endpoints

@router.post("/verification", response_model=VerificationResultResponse)
async def verify_task(
    verification_data: VerificationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> VerificationResultResponse:
    """Verify a task or step."""
    from app.models.verification_result import VerificationResult

    task = await task_service.get_task_by_id(db, verification_data.task_id, current_user.id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    verified = task.status == "completed"
    confidence = 1.0 if verified else 0.0

    db_verification = VerificationResult(
        task_id=verification_data.task_id,
        step_id=verification_data.step_id,
        verified=verified,
        confidence=confidence,
        evidence=[{"type": "status_check", "passed": verified}],
        failed_checks=[],
        verification_type=verification_data.verification_type or "basic",
        details={"task_status": task.status},
    )
    db.add(db_verification)
    await db.commit()
    await db.refresh(db_verification)

    return VerificationResultResponse(
        id=db_verification.id,
        task_id=db_verification.task_id,
        step_id=db_verification.step_id,
        verified=db_verification.verified,
        confidence=db_verification.confidence,
        evidence=db_verification.evidence,
        failed_checks=db_verification.failed_checks,
        verification_type=db_verification.verification_type,
        details=db_verification.details,
        created_at=db_verification.created_at,
    )


@router.get("/tasks/{task_id}/verification", response_model=list[VerificationResultResponse])
async def get_task_verifications(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[VerificationResultResponse]:
    """Get verification results for a task."""
    from app.models.verification_result import VerificationResult

    task = await task_service.get_task_by_id(db, task_id, current_user.id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    from sqlalchemy import select
    result = await db.execute(
        select(VerificationResult)
        .where(VerificationResult.task_id == task_id)
        .order_by(VerificationResult.created_at.desc())
    )
    verifications = result.scalars().all()

    return [
        VerificationResultResponse(
            id=v.id,
            task_id=v.task_id,
            step_id=v.step_id,
            verified=v.verified,
            confidence=v.confidence,
            evidence=v.evidence,
            failed_checks=v.failed_checks,
            verification_type=v.verification_type,
            details=v.details,
            created_at=v.created_at,
        )
        for v in verifications
    ]
