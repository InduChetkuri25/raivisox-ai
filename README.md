# RAIVISOX AI

**Next-Gen Adaptive Digital Intelligence**

_From Intent to Verified Outcome_

---

## Overview

RAIVISOX AI is an adaptive digital intelligence platform that transforms natural-language human intent into real, completed, and verified digital outcomes. Unlike simple AI chatbots, RAIVISOX orchestrates specialized AI models and digital tools to execute complex workflows, recover from failures, and verify completed work.

The platform follows a structured execution philosophy:

**UNDERSTAND → PLAN → ADAPT → OPTIMIZE → EXECUTE → OBSERVE → RECOVER → VERIFY → DELIVER → LEARN**

---

## Why RAIVISOX?

Most AI assistants generate answers. RAIVISOX executes real outcomes.

The central differentiator is **Adaptive Execution Intelligence** — the ability to dynamically determine the best execution strategy based on:

- Success probability
- Speed
- Cost
- Reliability
- Risk
- Available capabilities

RAIVISOX is:
- **Model-agnostic** — works with multiple AI providers
- **Tool-agnostic** — integrates with diverse digital tools
- **Verification-first** — never claims success without evidence
- **Recovery-aware** — diagnoses and recovers from failures intelligently

---

## Core Capabilities

### Implemented (Phase 1)
- Multi-model AI provider abstraction
- Task management with execution states
- Project workspaces
- Command center interface
- Basic execution engine
- Security kernel with permissions
- PostgreSQL database with migrations
- REST API with typed schemas

### In Development
- Intent engine (structured parsing)
- Task decomposer
- Multi-strategy planner
- Execution optimizer
- Verification engine (basic)

### Planned
- Voice interface
- Computer/browser control
- Creative studio (image, video, voice)
- App factory (full application generation)
- App guardian (maintenance)
- Background tasks and scheduler
- RAIVISOX-Bench evaluation framework
- Strategy learning and optimization
- Memory system

---

## Architecture

```
                         RAIVISOX AI
                              |
                    USER INTERACTION LAYER
                    (Text/Voice/Multimodal)
                              |
                        INTENT ENGINE
                              |
                    CONTEXT / MEMORY
                              |
                    TASK DECOMPOSER
                              |
                   DEFINITION OF DONE
                              |
                MULTI-STRATEGY PLANNER
                              |
                  STRATEGY EVALUATOR
                              |
               MODEL + TOOL ORCHESTRATOR
                              |
                 EXECUTION OPTIMIZER
                              |
                TASK GRAPH EXECUTION
                              |
             +----------------+----------------+
             |                |                |
          AI MODELS        COMPUTER           TOOLS
             |              CONTROL            |
             |                |                |
             +----------------+----------------+
                              |
                         OBSERVATION
                              |
                     VERIFICATION ENGINE
                         /           \
                    SUCCESS          FAILURE
                       |                |
                    DELIVER          RECOVERY
```

### Services

- **Intent Engine** (`services/intent/`) — Natural language to structured objectives
- **Task Decomposer** (`services/decomposer/`) — Dynamic task graph creation
- **Planner** (`services/planner/`) — Multi-strategy planning
- **Model Router** (`services/router/`) — Provider-independent AI abstraction
- **Execution Engine** (`services/execution/`) — Task graph execution
- **Verification Engine** (`services/verification/`) — Outcome validation
- **Recovery Engine** (`services/recovery/`) — Failure diagnosis and recovery
- **Security Kernel** (`services/security/`) — Permission and access control
- **Memory** (`services/memory/`) — User-controlled memory system
- **Scheduler** (`services/scheduler/`) — Background and scheduled tasks
- **Benchmark** (`services/benchmark/`) — RAIVISOX-Bench evaluation

### Integrations

- **Models** (`integrations/models/`) — OpenAI, Anthropic, Google, local models
- **Tools** (`integrations/tools/`) — Calculator, filesystem, HTTP, search
- **Browser** (`integrations/browser/`) — Browser automation
- **Voice** (`integrations/voice/`) — Speech-to-text, text-to-speech
- **Image/Video** (`integrations/image/`, `integrations/video/`) — Media generation
- **Cloud** (`integrations/cloud/`) — Cloud service integrations

---

## Tech Stack

### Frontend
- **Next.js 15** — React framework
- **React 19** — UI library
- **TypeScript** — Type safety
- **Tailwind CSS** — Styling
- **WebSockets/SSE** — Real-time updates

### Backend
- **Python 3.14** — Runtime
- **FastAPI** — Web framework
- **Pydantic** — Data validation
- **PostgreSQL** — Database
- **Redis** — Queue/cache
- **SQLAlchemy** — ORM
- **Alembic** — Migrations

### Infrastructure
- **Docker** — Containerization
- **docker-compose** — Multi-container orchestration
- **Environment-based configuration**

---

## Installation

### Prerequisites

- Node.js 20+ (tested with v25)
- Python 3.11+ (tested with v3.14)
- Docker (optional, for containerized setup)
- PostgreSQL (or use Docker)

### Quick Start (Local Development)

```bash
# Clone and setup
git clone <repository-url> raivisox
cd raivisox

# Install backend dependencies
cd apps/api
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt

# Install frontend dependencies
cd ../web
npm install

# Run database migrations
cd ../api
alembic upgrade head

# Start backend (from apps/api; the app module is app.main)
uvicorn app.main:app --reload --port 8000

# Start frontend (from apps/web, in another terminal)
npm run dev
```

The application will be available at:
- Frontend: http://localhost:3000
- API: http://localhost:8000
- API Docs: http://localhost:8000/docs

### Docker Setup

The compose files (`docker-compose.yml` for development,
`docker-compose.prod.yml` for production) and both Dockerfiles are
provided as the documented container path — **the container stack has
not been verified in this release environment** (Docker unavailable);
production was verified natively with a real PostgreSQL 16.9 and
`ENVIRONMENT=production` (see `docs/deployment.md` and `CHANGELOG.md`).

```bash
# Start all services
# Production: cp .env.example .env, fill in every required value, then:
docker compose -f docker-compose.prod.yml up -d --build

# View logs
docker compose -f docker-compose.prod.yml logs -f

# Stop services
docker compose -f docker-compose.prod.yml down
```

---

## Environment Variables

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Required variables:
- `DATABASE_URL` — PostgreSQL connection string
- `REDIS_URL` — Redis connection string
- `SECRET_KEY` — Application secret for JWT
- `ALLOWED_HOSTS` — Allowed host headers

Optional AI provider variables:
- `OPENAI_API_KEY` — OpenAI API key
- `ANTHROPIC_API_KEY` — Anthropic API key
- `GOOGLE_API_KEY` — Google API key

See `.env.example` for the complete list.

---

## Usage

### Via Web Interface

1. Open http://localhost:3000
2. Create an account or log in
3. Use the Command Center to describe what you want
4. Monitor task progress in real-time
5. View results and execution timeline

### Via API

```bash
# Create a task
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"title": "Build an expense tracker", "description": "Create a simple expense tracking web app for college students"}'

# Get task status
curl http://localhost:8000/api/v1/tasks/<task_id> \
  -H "Authorization: Bearer <token>"

# Stream task events
curl http://localhost:8000/api/v1/tasks/<task_id>/events \
  -H "Authorization: Bearer <token>" \
  -H "Accept: text/event-stream"
```

### Example Tasks

**Build an application:**
```
"Build me a clinic appointment application."
```

RAIVISOX will:
- Understand requirements
- Create Definition of Done
- Plan the task graph
- Select models and tools
- Generate the application
- Build and test it
- Verify the result
- Deliver the completed application

**Fix an issue:**
```
"Open my project and fix the login issue shown in this screenshot."
```

RAIVISOX will:
- Read and understand the screenshot
- Locate the project
- Inspect relevant code
- Diagnose the problem
- Fix the code
- Run tests
- Verify the fix
- Report the result

---

## Architecture Details

### Database Models

- **Users** — Authentication and profile
- **Projects** — Project workspaces
- **Tasks** — Execution units with states
- **TaskSteps** — Individual steps within tasks
- **TaskEvents** — Timeline events
- **Models** — AI model registry
- **ModelMetrics** — Performance tracking
- **Tools** — Tool definitions
- **Skills** — Plugin system
- **Executions** — Execution records
- **VerificationResults** — Verification evidence
- **RecoveryAttempts** — Recovery tracking
- **Memory** — User-controlled memory
- **Permissions** — Access control
- **AuditLogs** — Security audit trail
- **Benchmarks** — Evaluation results
- **ScheduledTasks** — Background scheduling

### Security Model

All actions pass through the Security Kernel:

- Permission scopes (FILES_READ, FILES_WRITE, BROWSER_INTERACT, etc.)
- Risk levels (LOW, MEDIUM, HIGH, CRITICAL)
- Confirmation gates for high-impact operations
- Audit logging
- Sandboxed tool execution
- Credential isolation
- Prompt injection defenses

---

## Testing

```bash
# Run all tests
cd tests
pytest

# Run unit tests
pytest unit/

# Run integration tests
pytest integration/

# Run with coverage
pytest --cov=services --cov-report=html
```

Test categories:
- Unit tests for intent parsing, task graph, model routing, strategy selection
- Integration tests for database, AI adapters, tool system
- End-to-end tests for realistic user journeys

---

## Benchmarking

RAIVISOX-Bench measures:

- Task success rate
- Verified success rate
- Latency
- Cost
- Recovery rate
- Human intervention rate
- False completion rate
- Model-routing performance
- Rollback success rate

See `docs/benchmark.md` for details.

---

## Roadmap

### Phase 1 — Foundation (CURRENT)
- [x] Repository structure
- [x] Frontend setup (Next.js + React + TypeScript + Tailwind)
- [x] Backend setup (FastAPI + PostgreSQL)
- [x] Database models and migrations
- [x] Authentication system
- [x] Command center UI
- [x] Basic task model
- [x] Docker setup
- [x] Model provider abstraction
- [x] Tool system foundation

### Phase 2 — Core Intelligence
- [ ] Intent Engine
- [ ] Task Decomposer
- [ ] Definition of Done
- [ ] Multi-Strategy Planner
- [ ] Model Router with selection logic
- [ ] Tool Router

### Phase 3 — Execution
- [ ] Execution engine
- [ ] Task graph execution
- [ ] Parallel execution
- [ ] Observation system
- [ ] Terminal sandbox
- [ ] Filesystem tools
- [ ] Browser abstraction

### Phase 4 — Reliability
- [ ] Verification engine
- [ ] Recovery engine
- [ ] Checkpoints
- [ ] Rollback system
- [ ] Audit trail

### Phase 5 — App Factory
- [ ] Requirements generation
- [ ] Architecture generation
- [ ] Code generation
- [ ] Build system
- [ ] Testing integration
- [ ] Debugging
- [ ] Packaging

### Phase 6 — Multimodal
- [ ] Voice interface
- [ ] Image generation
- [ ] Video generation
- [ ] Screenshot handling
- [ ] Document processing

### Phase 7 — Advanced Intelligence
- [ ] Strategy learning
- [ ] Performance optimization
- [ ] Background tasks
- [ ] Memory system
- [ ] Scheduler
- [ ] Predictive preparation

### Phase 8 — Production
- [ ] Security hardening
- [ ] Observability dashboard
- [ ] Benchmark system
- [ ] Deployment automation
- [ ] Scalability
- [ ] Performance optimization

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines.

---

## Security

See [SECURITY.md](SECURITY.md) for security policies and reporting.

---

## License

RAIVISOX AI is proprietary software. See [LICENSE](LICENSE) for details.

---

## Contact

For questions, feature requests, or security issues, please open an issue in the repository.

---

**RAIVISOX AI** — From Intent to Verified Outcome.
