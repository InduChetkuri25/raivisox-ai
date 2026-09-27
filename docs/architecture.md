# RAIVISOX AI — Architecture

**Next-Gen Adaptive Digital Intelligence**

_From Intent to Verified Outcome_

---

## 1. System Overview

RAIVISOX is a modular, service-oriented platform that orchestrates AI models and digital tools to execute complex workflows. The architecture separates concerns into distinct services while maintaining a unified execution model.

---

## 2. High-Level Architecture

```
                         RAIVISOX AI
                              |
                    ┌─────────┴─────────┐
                    │   Web Frontend    │
                    │   (Next.js/React) │
                    └─────────┬─────────┘
                              │
                    ┌─────────┴─────────┐
                    │   API Gateway     │
                    │   (FastAPI)       │
                    └─────────┬─────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   ┌────▼────┐         ┌─────▼─────┐        ┌─────▼─────┐
   │ Intent  │         │ Execution │        │  Security │
   │ Service │         │  Engine   │        │  Kernel   │
   └────┬────┘         └─────┬─────┘        └───────────┘
        │                     │
   ┌────▼────┐         ┌─────▼─────┐
   │ Planner │         │   Tools   │
   └────┬────┘         │  System   │
        │              └─────┬─────┘
   ┌────▼────┐               │
   │ Model   │        ┌──────┴──────┐
   │ Router  │        │ Integrations│
   └─────────┘        │  (Models,   │
                      │   Browser,  │
                      │   etc.)     │
                      └─────────────┘
```

---

## 3. Frontend Architecture

### Technology Stack
- **Framework**: Next.js 15 (App Router)
- **UI Library**: React 19
- **Language**: TypeScript
- **Styling**: Tailwind CSS
- **State Management**: React Context + SWR/React Query for server state
- **Real-time**: Server-Sent Events (SSE) for task updates

### Main Layout

```
apps/web/
├── app/
│   ├── layout.tsx           # Root layout with providers
│   ├── page.tsx             # Command center landing
│   ├── globals.css          # Global styles
│   ├── components/
│   │   ├── ui/              # Base UI components
│   │   ├── command/         # Command center components
│   │   ├── tasks/           # Task display components
│   │   ├── projects/        # Project workspace components
│   │   ├── dashboard/       # Observability dashboard
│   │   └── shared/         # Shared layout components
│   └── lib/
│       ├── api.ts           # API client
│       └── types.ts         # Frontend types
├── public/                  # Static assets
└── package.json
```

### Key Components

**CommandCenter** — Main interaction interface
- Text input for natural language commands
- Voice input button
- File/image upload
- URL input
- Screenshot attachment

**TaskTimeline** — Real-time task progress display
- Current state indicator
- Step-by-step execution log
- Model/tool usage display
- Verification status

**ProjectWorkspace** — Project management view
- File browser
- Task list
- Activity timeline
- Build/test status

---

## 4. Backend Architecture

### Technology Stack
- **Framework**: FastAPI
- **Language**: Python 3.14
- **ORM**: SQLAlchemy
- **Migrations**: Alembic
- **Validation**: Pydantic
- **Async**: asyncio throughout
- **Authentication**: JWT

### Service Structure

```
apps/api/
├── main.py                  # Application entry point
├── app/
│   ├── __init__.py
│   ├── config.py            # Configuration
│   ├── database.py          # Database connection
│   ├── dependencies.py      # FastAPI dependencies
│   ├── auth/                # Authentication
│   │   ├── router.py
│   │   ├── service.py
│   │   └── models.py
│   ├── tasks/               # Task management
│   │   ├── router.py
│   │   ├── service.py
│   │   ├── models.py
│   │   └── schemas.py
│   ├── projects/            # Project management
│   ├── models/              # AI model registry
│   ├── tools/               # Tool system
│   ├── execution/           # Execution engine
│   ├── verification/        # Verification engine
│   ├── recovery/            # Recovery engine
│   ├── security/            # Security kernel
│   └── api/
│       └── v1/
│           ├── router.py    # API version router
│           ├── endpoints/
│           └── schemas/
├── alembic/
│   ├── versions/
│   └── env.py
└── requirements.txt
```

---

## 5. Service Modules

### 5.1 Intent Service (`services/intent/`)

Converts natural language to structured objectives.

**Responsibilities:**
- Parse user input
- Extract goal and requirements
- Identify task type
- Assess risk level
- Determine required capabilities
- Flag ambiguities
- Generate clarification requests when needed

**Output Schema:**
```python
class IntentResult(BaseModel):
    goal: str
    task_type: TaskType
    requirements: List[str]
    constraints: List[str]
    context: Dict[str, Any]
    required_capabilities: List[Capability]
    risk_level: RiskLevel
    dependencies: List[str]
    definition_of_done: DefinitionOfDone
    ambiguity_detected: bool
    clarification_needed: Optional[str]
```

### 5.2 Task Decomposer (`services/decomposer/`)

Creates dynamic task graphs.

**Responsibilities:**
- Break down complex goals into tasks
- Identify dependencies
- Determine parallelizable tasks
- Assign priorities
- Set timeouts
- Define verification criteria per task

**Task Model:**
```python
class TaskNode(BaseModel):
    id: str
    parent_id: Optional[str]
    goal: str
    description: str
    dependencies: List[str]
    status: TaskStatus
    priority: int
    required_capabilities: List[Capability]
    selected_model: Optional[str]
    selected_tools: List[str]
    risk: RiskLevel
    timeout: Optional[int]
    result: Optional[Any]
    verification: VerificationCriteria
    metrics: TaskMetrics
```

### 5.3 Planner (`services/planner/`)

Multi-strategy planning with evaluation.

**Responsibilities:**
- Generate multiple execution strategies
- Evaluate each strategy
- Select optimal strategy based on mode
- Calculate estimated metrics

**Strategy Evaluation:**
```python
class Strategy(BaseModel):
    id: str
    name: str
    description: str
    steps: List[TaskNode]
    estimated_success: float
    estimated_latency: float
    estimated_cost: float
    risk_level: RiskLevel
    tool_availability: Dict[str, bool]
    model_suitability: Dict[str, float]
```

### 5.4 Model Router (`services/router/`)

Provider-independent AI abstraction.

**Responsibilities:**
- Route requests to appropriate provider
- Select optimal model for task
- Handle failures and fallbacks
- Track usage and costs
- Cache where appropriate

**Provider Interface:**
```python
class AIProvider(Protocol):
    async def chat_complete(self, request: ChatRequest) -> ChatResponse: ...
    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...
    async def generate_image(self, request: ImageRequest) -> ImageResponse: ...
    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse: ...
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...
```

### 5.5 Execution Engine (`services/execution/`)

Task graph execution.

**Responsibilities:**
- Execute task graph
- Handle parallel execution
- Coordinate tools and models
- Track progress
- Handle timeouts
- Emit events

**Execution Flow:**
```
1. Receive task graph
2. Check dependencies
3. Execute ready tasks (in parallel where possible)
4. Update task states
5. Emit events
6. Handle completion/failure
7. Return results
```

### 5.6 Tool System (`integrations/tools/`)

Standardized tool interface.

**Tool Interface:**
```python
class BaseTool(Protocol):
    id: str
    name: str
    description: str
    input_schema: JsonSchema
    output_schema: JsonSchema
    permissions: List[Permission]
    risk_level: RiskLevel
    available: bool

    async def execute(self, parameters: Dict[str, Any]) -> ToolResult: ...
```

**Available Tools (Phase 1):**
- Calculator
- File reader
- File writer
- Directory listing
- HTTP client
- Terminal sandbox

### 5.7 Verification Engine (`services/verification/`)

Outcome validation.

**Responsibilities:**
- Define verification criteria
- Execute verification checks
- Collect evidence
- Calculate confidence
- Report results

**Verification Result:**
```python
class VerificationResult(BaseModel):
    verified: bool
    confidence: float
    evidence: List[EvidenceItem]
    failed_checks: List[FailedCheck]
    verified_at: datetime
```

### 5.8 Recovery Engine (`services/recovery/`)

Failure diagnosis and recovery.

**Responsibilities:**
- Classify failures
- Diagnose root cause
- Generate alternatives
- Rank alternatives
- Execute recovery
- Track recovery success

**Failure Categories:**
- ModelFailure
- ToolFailure
- NetworkFailure
- Timeout
- UIStateChange
- ValidationError
- PermissionFailure
- ExternalServiceFailure
- AmbiguousInstruction

### 5.9 Security Kernel (`services/security/`)

Centralized security control.

**Responsibilities:**
- Permission validation
- Risk assessment
- Confirmation gates
- Audit logging
- Sandbox enforcement
- Credential isolation

---

## 6. Data Flow

### Task Creation Flow
```
User Input
    ↓
Intent Service (parse, understand)
    ↓
Task Decomposer (create task graph)
    ↓
Planner (select strategy)
    ↓
Model Router (select models)
    ↓
Tool Router (select tools)
    ↓
Security Kernel (validate permissions)
    ↓
Execution Engine (execute)
    ↓
Verification Engine (verify)
    ↓
Result + Evidence
```

### Real-time Updates
```
Execution Engine
    ↓
Event Bus
    ↓
SSE Publisher
    ↓
Frontend (task stream)
```

---

## 7. Database Schema

### Core Tables

**users**
- id, email, hashed_password, created_at, updated_at

**projects**
- id, user_id, name, description, created_at, updated_at

**tasks**
- id, project_id, user_id, title, description, status, goal, mode, created_at, updated_at, started_at, completed_at

**task_steps**
- id, task_id, parent_id, title, description, status, order, dependencies, model_used, tools_used, timeout, result, created_at, updated_at

**task_events**
- id, task_id, step_id, event_type, payload, created_at

**model_metrics**
- id, provider, model_name, task_type, latency, cost, success, timestamp

**tool_usage**
- id, task_id, tool_id, parameters, result, duration, timestamp

**verification_results**
- id, task_id, step_id, verified, confidence, evidence, failed_checks, created_at

**recovery_attempts**
- id, task_id, step_id, failure_type, diagnosis, strategy, success, created_at

**audit_logs**
- id, user_id, action, resource_type, resource_id, permissions_required, risk_level, success, details, created_at

**memory**
- id, user_id, type, key, value, created_at, updated_at

---

## 8. Integration Architecture

### AI Providers

```
User Request
    ↓
Model Router
    ↓
┌─────────────────┐
│  Provider Abstraction Layer │
├─────────────────┤
│ OpenAI Adapter  │
│ Anthropic Adapter│
│ Google Adapter  │
│ Local Adapter   │
│ Mock Adapter    │
└─────────────────┘
    ↓
Provider API
```

### Tool Adapters

```
Execution Request
    ↓
Tool System
    ↓
┌─────────────────┐
│  Tool Abstraction Layer │
├─────────────────┤
│ Calculator      │
│ File System     │
│ HTTP Client     │
│ Terminal Sandbox│
│ Browser Adapter │
└─────────────────┘
    ↓
System Resources
```

---

## 9. Configuration

Configuration via environment variables:

```python
# config.py
class Settings(BaseSettings):
    database_url: str
    redis_url: str
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # AI Providers
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    google_api_key: Optional[str] = None

    # Execution
    default_execution_mode: str = "BALANCED"
    max_execution_budget: Optional[float] = None
    default_task_timeout: int = 300

    # Security
    allowed_hosts: List[str] = []
    cors_origins: List[str] = []
```

---

## 10. Observability

### Logging
- Structured JSON logging
- Correlation IDs for request tracing
- Task-level logging context

### Metrics
- Task success/failure rates
- Model latency and cost
- Tool usage statistics
- Recovery rates

### Tracing
- Task execution traces
- Model call traces
- Tool call traces

---

## 11. Extensibility

### Plugin System (Skills)

Skills are addable capabilities:

```python
class Skill(BaseModel):
    name: str
    description: str
    required_tools: List[str]
    required_models: List[str]
    permissions: List[Permission]
    input_schema: JsonSchema
    output_schema: JsonSchema
    execute: Callable
    verify: Optional[Callable]
```

New skills can be added without modifying the core execution engine.

---

## 12. Deployment

### Docker Compose Services

```yaml
services:
  api:
    build: ./apps/api
    environment:
      - DATABASE_URL=postgresql://...
      - REDIS_URL=redis://...
  web:
    build: ./apps/web
    ports:
      - "3000:3000"
  postgres:
    image: postgres:16
  redis:
    image: redis:7
```

---

## 13. Development Phases Architecture Impact

### Phase 1 — Foundation
Core services, database, authentication, basic UI, model abstraction, tool foundation.

### Phase 2 — Core Intelligence
Full intent engine, task decomposer, planner, model selection.

### Phase 3 — Execution
Full execution engine, parallel execution, terminal sandbox, browser abstraction.

### Phase 4 — Reliability
Verification engine, recovery engine, checkpoints, rollback.

### Phase 5-8
App factory, multimodal, advanced intelligence, production hardening.
