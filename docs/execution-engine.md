# RAIVISOX Execution Engine

## Overview

The Execution Engine is responsible for executing task graphs, coordinating AI models and tools, and managing the execution lifecycle from start to verification.

---

## Architecture

```
Task Graph
    ↓
Dependency Resolver
    ↓
Executor Pool (Parallel)
    ↓
Tool/Model Orchestrator
    ↓
Observation System
    ↓
Verification Engine
    ↓
Results + Evidence
```

---

## Execution States

### Task States

- `CREATED` - Task created, awaiting execution
- `UNDERSTANDING` - Analyzing intent and requirements
- `PLANNING` - Creating execution plan
- `PREPARING` - Setting up prerequisites
- `WAITING_FOR_USER` - Awaiting user input/confirmation
- `EXECUTING` - Currently executing
- `VERIFYING` - Verifying outcomes
- `RECOVERING` - Recovering from failure
- `COMPLETED` - Successfully completed
- `PARTIALLY_COMPLETED` - Completed with some issues
- `FAILED` - Failed
- `CANCELLED` - Cancelled by user

### Step States

- `PENDING` - Awaiting execution
- `CREATED` - Step created
- `UNDERSTANDING` - Analyzing step requirements
- `PLANNING` - Planning step execution
- `PREPARING` - Preparing for execution
- `WAITING_FOR_USER` - Awaiting user input
- `EXECUTING` - Currently executing
- `VERIFYING` - Verifying results
- `RECOVERING` - Recovering from failure
- `COMPLETED` - Completed successfully
- `PARTIALLY_COMPLETED` - Completed with issues
- `FAILED` - Failed
- `CANCELLED` - Cancelled

---

## Execution Flow

### 1. Task Reception
- Receive task with goal and parameters
- Validate task input
- Create initial task state

### 2. Understanding Phase
- Analyze task intent
- Extract requirements
- Identify constraints
- Determine risk level
- Request clarification if needed

### 3. Planning Phase
- Generate task graph
- Identify dependencies
- Determine parallelizable tasks
- Select execution mode
- Estimate resources

### 4. Preparation Phase
- Set up execution environment
- Prepare tools and models
- Validate prerequisites
- Create checkpoints

### 5. Execution Phase
- Execute steps in dependency order
- Run independent steps in parallel
- Coordinate models and tools
- Monitor progress
- Handle timeouts

### 6. Verification Phase
- Execute verification checks
- Collect evidence
- Calculate confidence
- Report results

### 7. Completion
- Update task status
- Store results
- Record metrics
- Emit completion events

---

## Parallel Execution

The execution engine identifies independent tasks that can run in parallel:

```python
# Example: Determine parallel execution groups
def get_execution_groups(task_graph):
    """Group tasks by dependency depth for parallel execution."""
    groups = []
    completed = set()

    while len(completed) < len(task_graph):
        # Find tasks whose dependencies are all completed
        ready = [
            task for task in task_graph
            if task.id not in completed
            and all(dep in completed for dep in task.dependencies)
        ]
        if not ready:
            break  # Deadlock or cycle detected
        groups.append(ready)
        completed.update(task.id for task in ready)

    return groups
```

---

## Tool Orchestration

Tools are selected and executed based on task requirements:

```python
async def execute_with_tools(step, tools, security_kernel):
    """Execute a step using appropriate tools."""
    # Check permissions
    if not security_kernel.check_permissions(step, tools):
        raise PermissionError("Insufficient permissions")

    results = []
    for tool in tools:
        # Validate parameters
        validated, error = tool.validate_parameters(step.parameters)
        if not validated:
            continue

        # Execute tool
        result = await tool.execute(step.parameters)

        # Record usage
        await record_tool_usage(step.task_id, tool, result)

        results.append(result)

    return results
```

---

## Model Selection

Models are selected based on task requirements:

```python
async def select_model(task, available_models, execution_mode):
    """Select the best model for a task."""
    candidates = []

    for model in available_models:
        if not model.available:
            continue

        # Score based on task requirements
        score = 0

        # Capability match
        if task.requires_vision and model.supports_vision:
            score += 30
        if task.requires_coding and model.category == 'coding':
            score += 20

        # Mode-based preferences
        if execution_mode == 'fast':
            score += 30 - (model.latency_estimate_ms / 100)
        elif execution_mode == 'quality':
            score += model.quality_rating * 30
        elif execution_mode == 'economy':
            score += max(0, 20 - model.cost_per_1k_tokens * 1000)

        candidates.append((model, score))

    # Select best candidate
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0] if candidates else None
```

---

## Timeout Handling

Each step has a configurable timeout:

```python
async def execute_with_timeout(step, execution_func, timeout):
    """Execute with timeout handling."""
    try:
        result = await asyncio.wait_for(
            execution_func(),
            timeout=timeout
        )
        return result
    except asyncio.TimeoutError:
        # Handle timeout
        await handle_timeout(step)
        raise
```

---

## Error Handling

Errors are classified and handled appropriately:

```python
class ExecutionError(Exception):
    def __init__(self, error_type, message, task_id, step_id=None,
                 recoverable=True, user_action_required=False):
        self.error_type = error_type
        self.message = message
        self.task_id = task_id
        self.step_id = step_id
        self.recoverable = recoverable
        self.user_action_required = user_action_required

# Error types
ERROR_TYPES = {
    'model_failure': {'recoverable': True},
    'tool_failure': {'recoverable': True},
    'network_failure': {'recoverable': True},
    'timeout': {'recoverable': True},
    'permission_failure': {'recoverable': False, 'user_action_required': True},
    'validation_failure': {'recoverable': False},
    'external_service_failure': {'recoverable': True},
}
```

---

## Event Emission

Events are emitted throughout execution for real-time tracking:

```python
async def emit_event(task_id, step_id, event_type, payload):
    """Emit an execution event."""
    event = TaskEvent(
        task_id=task_id,
        step_id=step_id,
        event_type=event_type,
        payload=payload
    )
    await db.create(event)

    # Also emit to SSE stream
    await sse_publisher.publish(task_id, event)
```

Event types:
- `created` - Task/step created
- `status_changed` - State transition
- `model_call` - AI model invoked
- `tool_call` - Tool executed
- `verification` - Verification check
- `recovery` - Recovery attempt
- `error` - Error occurred
- `completed` - Task/step completed

---

## Metrics Collection

Execution metrics are recorded for observability:

```python
metrics = {
    'planning_time_ms': planning_end - planning_start,
    'execution_time_ms': execution_end - execution_start,
    'model_time_ms': sum(call.duration for call in model_calls),
    'tool_time_ms': sum(call.duration for call in tool_calls),
    'total_time_ms': total_end - total_start,
    'model_calls': len(model_calls),
    'tool_calls': len(tool_calls),
    'cost_usd': total_cost,
    'recovery_attempts': recovery_count,
}
```

---

## Current Implementation (Phase 1)

The Phase 1 implementation provides:

- Basic task creation and status management
- Simple execution via `/api/v1/tasks/{id}/execute`
- Task event tracking
- Step creation and management
- Basic verification support

Phase 2 will add:
- Full intent understanding
- Dynamic task graph generation
- Parallel execution
- Intelligent model and tool selection
