# RAIVISOX AI API Documentation

## Overview

The RAIVISOX API provides programmatic access to the adaptive digital intelligence platform.

**Base URL:** `http://localhost:8000`

**Version:** 1.0.0

---

## Authentication

All API endpoints (except auth and health) require a Bearer token.

### Login

```bash
POST /api/v1/auth/login
```

Request:
```json
{
  "email": "user@example.com",
  "password": "your-password"
}
```

Response:
```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 86400
}
```

### Get Current User

```bash
GET /api/v1/auth/me
Authorization: Bearer <token>
```

---

## Tasks

### Create Task

```bash
POST /api/v1/tasks
Authorization: Bearer <token>
```

Request:
```json
{
  "title": "Build a clinic appointment application",
  "description": "Create a web application for managing clinic appointments",
  "project_id": null,
  "mode": "balanced"
}
```

Response: Task object

### List Tasks

```bash
GET /api/v1/tasks?page=1&page_size=20&status=completed
Authorization: Bearer <token>
```

### Get Task

```bash
GET /api/v1/tasks/{task_id}
Authorization: Bearer <token>
```

### Update Task

```bash
PUT /api/v1/tasks/{task_id}
Authorization: Bearer <token>
```

### Cancel Task

```bash
POST /api/v1/tasks/{task_id}/cancel
Authorization: Bearer <token>
```

### Resume Task

```bash
POST /api/v1/tasks/{task_id}/resume
Authorization: Bearer <token>
```

### Execute Task

```bash
POST /api/v1/tasks/{task_id}/execute
Authorization: Bearer <token>
```

### Get Task Events

```bash
GET /api/v1/tasks/{task_id}/events?page=1&page_size=100
Authorization: Bearer <token>
```

---

## Projects

### Create Project

```bash
POST /api/v1/projects
Authorization: Bearer <token>
```

Request:
```json
{
  "name": "My Project",
  "description": "Project description"
}
```

### List Projects

```bash
GET /api/v1/projects?page=1&page_size=20
Authorization: Bearer <token>
```

### Get Project

```bash
GET /api/v1/projects/{project_id}
Authorization: Bearer <token>
```

### Update Project

```bash
PUT /api/v1/projects/{project_id}
Authorization: Bearer <token>
```

### Delete Project

```bash
DELETE /api/v1/projects/{project_id}
Authorization: Bearer <token>
```

---

## Task Steps

### Create Step

```bash
POST /api/v1/tasks/{task_id}/steps
Authorization: Bearer <token>
```

Request:
```json
{
  "title": "Set up database",
  "description": "Create PostgreSQL schema",
  "order": 0
}
```

### Update Step

```bash
PUT /api/v1/tasks/{task_id}/steps/{step_id}
Authorization: Bearer <token>
```

---

## Verification

### Verify Task

```bash
POST /api/v1/verification
Authorization: Bearer <token>
```

Request:
```json
{
  "task_id": "task-id",
  "verification_type": "basic"
}
```

Response:
```json
{
  "id": "verification-id",
  "task_id": "task-id",
  "verified": true,
  "confidence": 0.95,
  "evidence": [...],
  "failed_checks": []
}
```

---

## Models

### List Available Models

```bash
GET /api/v1/models
Authorization: Bearer <token>
```

Response:
```json
[
  {
    "id": "gpt-4",
    "name": "GPT-4",
    "provider": "openai",
    "category": "reasoning",
    "available": true
  }
]
```

---

## Tools

### List Available Tools

```bash
GET /api/v1/tools
Authorization: Bearer <token>
```

Response:
```json
[
  {
    "id": "calculator",
    "name": "Calculator",
    "description": "Perform mathematical calculations",
    "permissions": ["external_api"],
    "risk_level": "low",
    "available": true
  }
]
```

---

## Error Handling

All errors return a consistent format:

```json
{
  "detail": "Error description"
}
```

HTTP Status Codes:
- `200` - Success
- `201` - Created
- `204` - No Content
- `400` - Bad Request
- `401` - Unauthorized
- `403` - Forbidden
- `404` - Not Found
- `422` - Validation Error
- `500` - Internal Server Error

---

## Rate Limiting

Rate limiting may be applied to prevent abuse. Headers included:
- `X-RateLimit-Limit`
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset`

---

## Webhooks / SSE

For real-time updates, use Server-Sent Events:

```javascript
const eventSource = new EventSource('/api/v1/tasks/{task_id}/events');

eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Task event:', data);
};
```
