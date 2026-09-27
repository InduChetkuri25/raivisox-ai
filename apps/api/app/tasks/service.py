"""Task and project services."""

from datetime import datetime
from typing import Optional

from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.task import Task, TaskStatus, ExecutionMode, RiskLevel
from app.models.task_step import TaskStep, StepStatus
from app.models.task_event import TaskEvent, EventType
from app.models.project import Project
from app.models.user import User
from app.tasks.schemas import (
    TaskCreate,
    TaskUpdate,
    TaskStepCreate,
    TaskStepUpdate,
    TaskResponse,
    TaskStepResponse,
    TaskEventResponse,
    ProjectCreate,
    ProjectUpdate,
    ProjectResponse,
)


async def create_project(
    db: AsyncSession,
    user_id: str,
    project_data: ProjectCreate,
) -> Project:
    """Create a new project."""
    db_project = Project(
        user_id=user_id,
        name=project_data.name,
        description=project_data.description or "",
    )
    db.add(db_project)
    await db.commit()
    await db.refresh(db_project)
    return db_project


async def get_project_by_id(
    db: AsyncSession,
    project_id: str,
    user_id: Optional[str] = None,
) -> Optional[Project]:
    """Get a project by ID."""
    query = select(Project).where(Project.id == project_id)

    if user_id:
        query = query.where(Project.user_id == user_id)

    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_projects(
    db: AsyncSession,
    user_id: str,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Project], int]:
    """Get projects for a user with pagination."""
    # Get total count
    count_query = select(func.count(Project.id)).where(Project.user_id == user_id)
    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    # Get projects
    offset = (page - 1) * page_size
    query = (
        select(Project)
        .where(Project.user_id == user_id)
        .order_by(Project.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await db.execute(query)
    projects = list(result.scalars().all())

    return projects, total


async def update_project(
    db: AsyncSession,
    project: Project,
    project_data: ProjectUpdate,
) -> Project:
    """Update a project."""
    update_data = project_data.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        setattr(project, field, value)

    await db.commit()
    await db.refresh(project)
    return project


async def delete_project(
    db: AsyncSession,
    project: Project,
) -> None:
    """Delete a project."""
    await db.delete(project)
    await db.commit()


async def create_task(
    db: AsyncSession,
    user_id: str,
    task_data: TaskCreate,
) -> Task:
    """Create a new task."""
    db_task = Task(
        user_id=user_id,
        project_id=task_data.project_id,
        title=task_data.title,
        description=task_data.description or "",
        goal=task_data.goal or task_data.title,
        status=TaskStatus.CREATED,
        mode=task_data.mode,
        risk_level=RiskLevel.LOW,
        definition_of_done=task_data.definition_of_done,
    )
    db.add(db_task)
    await db.commit()
    await db.refresh(db_task)

    # Create initial event
    await create_task_event(
        db,
        task_id=db_task.id,
        event_type=EventType.CREATED,
        payload={"title": db_task.title},
    )

    # Re-fetch with relationships eagerly loaded so serialization
    # (task.steps / task.events) never triggers a lazy load in async context.
    return await get_task_by_id(db, db_task.id)


async def get_task_by_id(
    db: AsyncSession,
    task_id: str,
    user_id: Optional[str] = None,
) -> Optional[Task]:
    """Get a task by ID with its steps and events."""
    query = (
        select(Task)
        .options(
            selectinload(Task.steps),
            selectinload(Task.events),
        )
        .where(Task.id == task_id)
    )

    if user_id:
        query = query.where(Task.user_id == user_id)

    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_tasks(
    db: AsyncSession,
    user_id: str,
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Task], int]:
    """Get tasks for a user with pagination."""
    # Build query
    query = select(Task).where(Task.user_id == user_id)

    if status:
        query = query.where(Task.status == status)
    if project_id:
        query = query.where(Task.project_id == project_id)

    # Get total count
    count_query = select(func.count(Task.id)).where(Task.user_id == user_id)
    if status:
        count_query = count_query.where(Task.status == status)
    if project_id:
        count_query = count_query.where(Task.project_id == project_id)

    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    # Get tasks
    offset = (page - 1) * page_size
    query = (
        query
        .options(
            selectinload(Task.steps),
            selectinload(Task.events),
        )
        .order_by(Task.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await db.execute(query)
    tasks = list(result.scalars().all())

    return tasks, total


async def update_task_status(
    db: AsyncSession,
    task: Task,
    status: TaskStatus | str,
    execution_run_id: Optional[str] = None,
) -> Task:
    """Update task status and emit event.

    ``execution_run_id`` tags the STATUS_CHANGED event with the first-class
    ExecutionRun that produced it; legacy callers (create/update, sweeps)
    pass nothing and the event stays unassociated.
    """
    old_status = task.status
    task.status = status.value if isinstance(status, TaskStatus) else status

    if task.status == TaskStatus.EXECUTING.value:
        task.started_at = datetime.utcnow()
    elif task.status in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
        task.completed_at = datetime.utcnow()

    await db.commit()
    await db.refresh(task)

    # Emit status change event
    await create_task_event(
        db,
        task_id=task.id,
        event_type=EventType.STATUS_CHANGED,
        payload={
            "old_status": old_status,
            "new_status": task.status,
        },
        execution_run_id=execution_run_id,
    )

    return task


async def update_task(
    db: AsyncSession,
    task: Task,
    task_data: TaskUpdate,
) -> Task:
    """Update a task."""
    update_data = task_data.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        if field == "status" and value:
            task.status = value
        else:
            setattr(task, field, value)

    await db.commit()
    await db.refresh(task)
    return task


async def cancel_task(
    db: AsyncSession,
    task: Task,
) -> Task:
    """Cancel a task."""
    task.status = TaskStatus.CANCELLED.value
    task.cancelled_at = datetime.utcnow()

    await db.commit()
    await db.refresh(task)

    # Emit cancellation event
    await create_task_event(
        db,
        task_id=task.id,
        event_type=EventType.STATUS_CHANGED.value,
        payload={"new_status": TaskStatus.CANCELLED.value},
    )

    return task


async def create_task_step(
    db: AsyncSession,
    task: Task,
    step_data: TaskStepCreate,
) -> TaskStep:
    """Create a new task step."""
    db_step = TaskStep(
        task_id=task.id,
        title=step_data.title,
        description=step_data.description or "",
        order=step_data.order,
        dependencies=step_data.dependencies,
        timeout=step_data.timeout,
        status=StepStatus.PENDING.value,
    )
    db.add(db_step)
    await db.commit()
    await db.refresh(db_step)
    return db_step


async def get_task_step_by_id(
    db: AsyncSession,
    step_id: str,
    task_id: Optional[str] = None,
) -> Optional[TaskStep]:
    """Get a task step by ID."""
    from sqlalchemy import select

    query = select(TaskStep).where(TaskStep.id == step_id)

    if task_id:
        query = query.where(TaskStep.task_id == task_id)

    result = await db.execute(query)
    return result.scalar_one_or_none()


async def update_task_step(
    db: AsyncSession,
    step: TaskStep,
    step_data: TaskStepUpdate,
) -> TaskStep:
    """Update a task step."""
    update_data = step_data.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        setattr(step, field, value)

    await db.commit()
    await db.refresh(step)
    return step


async def update_task_step_status(
    db: AsyncSession,
    step: TaskStep,
    status: StepStatus,
) -> TaskStep:
    """Update task step status."""
    step.status = status
    await db.commit()
    await db.refresh(step)
    return step


async def create_task_event(
    db: AsyncSession,
    task_id: str,
    event_type: str | EventType,
    step_id: Optional[str] = None,
    payload: Optional[dict] = None,
    execution_run_id: Optional[str] = None,
) -> TaskEvent:
    """Create a task event.

    ``execution_run_id`` associates NEW events with their first-class
    ExecutionRun. It stays Optional and is never fabricated: events written
    before Execution Runs existed (and sweep events without a run row)
    remain valid with NULL.
    """
    db_event = TaskEvent(
        task_id=task_id,
        step_id=step_id,
        event_type=event_type.value if hasattr(event_type, "value") else event_type,
        payload=payload or {},
        execution_run_id=execution_run_id,
    )
    db.add(db_event)
    await db.commit()
    await db.refresh(db_event)
    return db_event


async def get_task_events(
    db: AsyncSession,
    task_id: str,
    page: int = 1,
    page_size: int = 100,
) -> list[TaskEvent]:
    """Get events for a task."""
    offset = (page - 1) * page_size
    query = (
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await db.execute(query)
    return list(result.scalars().all())


async def delete_task(
    db: AsyncSession,
    task: Task,
) -> None:
    """Delete a task."""
    await db.delete(task)
    await db.commit()


# Conversion functions

def task_to_response(task: Task) -> TaskResponse:
    """Convert a Task model to a response schema."""
    return TaskResponse(
        id=task.id,
        user_id=task.user_id,
        project_id=task.project_id,
        title=task.title,
        description=task.description,
        goal=task.goal,
        status=task.status,
        mode=task.mode,
        risk_level=task.risk_level,
        outcome=getattr(task, "outcome", None),
        definition_of_done=task.definition_of_done,
        result=task.result,
        error=task.error,
        started_at=task.started_at,
        completed_at=task.completed_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
        steps=[TaskStepResponse(
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
        ) for step in task.steps],
        events=[TaskEventResponse(
            id=event.id,
            task_id=event.task_id,
            step_id=event.step_id,
            event_type=event.event_type,
            payload=event.payload,
            execution_run_id=event.execution_run_id,
            created_at=event.created_at,
        ) for event in task.events],
    )


def project_to_response(project: Project) -> ProjectResponse:
    """Convert a Project model to a response schema."""
    return ProjectResponse(
        id=project.id,
        user_id=project.user_id,
        name=project.name,
        description=project.description,
        status=project.status,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )
