# Contributing to RAIVISOX AI

Thank you for your interest in contributing to RAIVISOX AI! This document provides guidelines for contributing to the project.

---

## Development Principles

1. **Make it real, not fake** - Don't create mock implementations that pretend to work. If a feature requires an unavailable external API, create a clean adapter interface with a development/mock implementation clearly marked as such.

2. **Build modular, extensible systems** - The system should be designed so future AI models, tools, and skills can be added without rewriting the core architecture.

3. **Test everything** - Never skip testing. Every feature should have appropriate tests.

4. **Security first** - All actions must pass through security controls. Never bypass authentication, authorization, or safety mechanisms.

5. **Documentation matters** - Update documentation when making changes.

---

## Development Setup

### Prerequisites

- Node.js 20+
- Python 3.11+
- PostgreSQL 15+
- Docker (optional)

### Local Development

```bash
# Clone the repository
git clone <repository-url> raivisox
cd raivisox

# Setup backend
cd apps/api
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head

# Setup frontend
cd ../web
npm install

# Start backend (from apps/api)
uvicorn app.main:app --reload --port 8000

# Start frontend (from apps/web, in another terminal)
npm run dev
```

### Using Docker

```bash
docker-compose up -d
```

---

## Architecture Overview

RAIVISOX is organized into:

- **apps/api/** - Python FastAPI backend
- **apps/web/** - Next.js React frontend
- **services/** - Core services (intent, planning, execution, etc.)
- **integrations/** - AI model and tool integrations
- **packages/** - Shared packages
- **tests/** - Test suites
- **docs/** - Documentation

---

## Development Phases

We follow an incremental development approach:

| Phase | Focus | Status |
|-------|-------|--------|
| Phase 1 | Foundation (backend, frontend, DB, auth) | In Progress |
| Phase 2 | Core Intelligence (intent, planning, routing) | Planned |
| Phase 3 | Execution (task graph, tools, sandbox) | Planned |
| Phase 4 | Reliability (verification, recovery) | Planned |
| Phase 5+ | Advanced features | Planned |

Do not skip phases. Each phase builds on the previous one.

---

## Code Style

### Python (Backend)

- Follow PEP 8
- Use type hints
- Use Pydantic models for data validation
- Write async code with asyncio
- Document public APIs with docstrings

### TypeScript (Frontend)

- Strict mode enabled
- Use TypeScript for all new code
- Follow React best practices
- Use Tailwind CSS for styling
- Keep components modular and reusable

---

## Testing

### Running Tests

```bash
# Backend tests
cd apps/api
pytest

# Frontend tests
cd apps/web
npm test
```

### Test Categories

1. **Unit tests** - Test individual functions and components
2. **Integration tests** - Test service interactions
3. **End-to-end tests** - Test complete user workflows

---

## Pull Request Process

1. Create a feature branch from `main`
2. Make your changes
3. Ensure all tests pass
4. Update documentation if needed
5. Submit a pull request

### PR Guidelines

- Keep PRs focused and small
- Include tests for new functionality
- Update relevant documentation
- Describe what the PR does and why

---

## Security

### Reporting Security Issues

Do not open public issues for security vulnerabilities. Instead, follow the process in [SECURITY.md](SECURITY.md).

### Security Principles

- Never hard-code credentials
- Use environment variables for sensitive configuration
- Validate all inputs
- Implement proper authentication and authorization
- Follow principle of least privilege

---

## Questions?

Open an issue for questions or discussion.

---

Thank you for contributing to RAIVISOX AI!
