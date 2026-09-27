import type { Project, TaskArtifact, User } from './types';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

/**
 * Real backend route map (see apps/api routers).
 * NOTE: auth routes live at `/auth/*` (NO `/api/v1` prefix); tasks/projects
 * are mounted under `/api/v1`.
 */
export const API_URLS = {
  login: '/auth/login',
  register: '/auth/register',
  me: '/auth/me',
} as const;

interface ApiOptions {
  token?: string;
  method?: string;
  body?: any;
}

export async function api<T>(endpoint: string, options: ApiOptions = {}): Promise<T> {
  const { token, method = 'GET', body } = options;

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };

  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const response = await fetch(`${API_BASE}${endpoint}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Request failed' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }

  // Handle streaming responses
  if (response.headers.get('content-type')?.includes('text/event-stream')) {
    return response as unknown as T;
  }

  return response.json();
}

// Auth API
export const authApi = {
  login: (email: string, password: string) =>
    api<{ access_token: string; token_type: string; expires_in: number }>(API_URLS.login, {
      method: 'POST',
      body: { email, password },
    }),

  register: (email: string, password: string, full_name?: string) =>
    api<{ id: string; email: string; full_name: string | null }>(API_URLS.register, {
      method: 'POST',
      body: { email, password, full_name },
    }),

  getMe: (token: string) =>
    api<User>(API_URLS.me, {
      token,
    }),

  updateMe: (token: string, data: { full_name?: string; email?: string }) =>
    api<{ id: string; email: string; full_name: string | null }>(API_URLS.me, {
      token,
      method: 'PUT',
      body: data,
    }),
};

// Projects API
export const projectsApi = {
  list: (token: string, page = 1, page_size = 20) =>
    api<{ projects: any[]; total: number; page: number; page_size: number }>(
      `/api/v1/projects?page=${page}&page_size=${page_size}`,
      { token }
    ),

  get: (token: string, projectId: string) =>
    api<Project>(
      `/api/v1/projects/${projectId}`,
      { token }
    ),

  create: (token: string, name: string, description?: string) =>
    api<Project>('/api/v1/projects', {
      token,
      method: 'POST',
      body: { name, description },
    }),

  update: (token: string, projectId: string, data: any) =>
    api<Project>(
      `/api/v1/projects/${projectId}`,
      { token, method: 'PUT', body: data }
    ),

  delete: (token: string, projectId: string) =>
    api<void>(`/api/v1/projects/${projectId}`, { token, method: 'DELETE' }),
};

// Tasks API
export const tasksApi = {
  list: (token: string, status?: string, project_id?: string, page = 1, page_size = 20) => {
    const params = new URLSearchParams({ page: page.toString(), page_size: page_size.toString() });
    if (status) params.set('status', status);
    if (project_id) params.set('project_id', project_id);
    return api<{ tasks: any[]; total: number; page: number; page_size: number }>(
      `/api/v1/tasks?${params}`,
      { token }
    );
  },

  get: (token: string, taskId: string) =>
    api<any>(`/api/v1/tasks/${taskId}`, { token }),

  create: (token: string, title: string, goal?: string, mode?: string) =>
    api<any>('/api/v1/tasks', {
      token,
      method: 'POST',
      body: { title, goal, mode },
    }),

  update: (token: string, taskId: string, data: any) =>
    api<any>(`/api/v1/tasks/${taskId}`, { token, method: 'PUT', body: data }),

  cancel: (token: string, taskId: string) =>
    api<any>(`/api/v1/tasks/${taskId}/cancel`, { token, method: 'POST' }),

  resume: (token: string, taskId: string) =>
    api<any>(`/api/v1/tasks/${taskId}/resume`, { token, method: 'POST' }),

  // Default: synchronous execution (resolves with the final pipeline result).
  // Pass `{ async_mode: true }` for 202-accept + background execution; the
  // 202 body is returned directly (no JSON unwrap needed on non-JSON 202 —
  // the backend always sends a JSON body).
  execute: (token: string, taskId: string, body?: Record<string, unknown>) =>
    api<any>(`/api/v1/tasks/${taskId}/execute`, {
      token,
      method: 'POST',
      body: body ?? {},
    }),

  events: (token: string, taskId: string) =>
    api<any[]>(`/api/v1/tasks/${taskId}/events`, { token }),
};

// NOTE: there is intentionally no EventSource/SSE helper. The backend exposes
// `/api/v1/tasks/{id}/events` as plain JSON, and the dashboard polls task
// state instead. The previous headerless EventSource helper never worked.

// Audit API (admin-only on the backend — the UI never bypasses it)
export const auditApi = {
  list: (token: string, action?: string, allowed?: boolean, limit = 200) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (action) params.set('action', action);
    if (allowed !== undefined) params.set('allowed', String(allowed));
    return api<{ events: any[]; count: number }>(`/api/v1/audit?${params}`, { token });
  },
};

// Artifacts API — files the pipeline actually wrote for a task.
export const artifactsApi = {
  list: (token: string, taskId: string, run?: string) =>
    api<{ task_id: string; artifacts: TaskArtifact[] }>(
      run
        ? `/api/v1/tasks/${taskId}/artifacts?run=${encodeURIComponent(run)}`
        : `/api/v1/tasks/${taskId}/artifacts`,
      { token }
    ),
  // Raw fetch so callers can hand the response to a blob download.
  download: (token: string, taskId: string, index: number) =>
    fetch(`${API_BASE}/api/v1/tasks/${taskId}/artifacts/${index}`, {
      headers: { Authorization: `Bearer ${token}` },
    }),
  // ZIP of all artifacts — same authorization, raw fetch for blob handling.
  downloadBundle: (token: string, taskId: string) =>
    fetch(`${API_BASE}/api/v1/tasks/${taskId}/artifacts/bundle/download`, {
      headers: { Authorization: `Bearer ${token}` },
    }),
  // ZIP of ALL artifacts across a project's tasks (owner-scoped).
  downloadProjectBundle: (token: string, projectId: string) =>
    fetch(`${API_BASE}/api/v1/projects/${projectId}/artifacts/bundle/download`, {
      headers: { Authorization: `Bearer ${token}` },
    }),
  // Persisted artifact history grouped by FIRST-CLASS execution run
  // (durable execution_runs rows — run_id/started_at/finished_at/status/
  // outcome come from the run row, not timestamp inference).
  history: (token: string, taskId: string) =>
    api<{
      task_id: string;
      runs: Array<{
        run_id: string;
        started_at: string | null;
        finished_at: string | null;
        status: string | null;
        outcome: string | null;
        is_legacy_run: boolean;
        last_artifact_at: string | null;
        artifact_count: number;
        current_count: number;
        artifacts: TaskArtifact[];
      }>;
    }>(`/api/v1/tasks/${taskId}/artifacts/history`, { token }),
  // Download by stable registry ID (works for historical/superseded rows).
  downloadById: (token: string, taskId: string, artifactId: string) =>
    fetch(`${API_BASE}/api/v1/tasks/${taskId}/artifacts/rows/${artifactId}`, {
      headers: { Authorization: `Bearer ${token}` },
    }),
};

