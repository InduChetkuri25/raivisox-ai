// User types
export interface User {
  id: string;
  email: string;
  full_name: string | null;
  is_active: boolean;
  is_verified: boolean;
  created_at: string;
}

export interface LoginCredentials {
  email: string;
  password: string;
}

export interface RegisterData {
  email: string;
  password: string;
  full_name?: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

// Project types
export interface Project {
  id: string;
  user_id: string;
  name: string;
  description: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface ProjectListResponse {
  projects: Project[];
  total: number;
  page: number;
  page_size: number;
}

// Task types
export type TaskStatus =
  | 'created'
  | 'understanding'
  | 'planning'
  | 'preparing'
  | 'waiting_for_user'
  | 'executing'
  | 'verifying'
  | 'recovering'
  | 'completed'
  | 'partially_completed'
  | 'failed'
  | 'cancelled';

export type ExecutionMode = 'fast' | 'balanced' | 'quality' | 'economy';

export interface Task {
  id: string;
  user_id: string;
  project_id: string | null;
  title: string;
  description: string | null;
  goal: string | null;
  status: TaskStatus;
  mode: string;
  risk_level: string;
  outcome: 'verified' | 'completed_unverified' | 'failed' | null;
  definition_of_done: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  steps: TaskStep[];
  events: TaskEvent[];
}

export interface TaskStep {
  id: string;
  task_id: string;
  parent_id: string | null;
  title: string;
  description: string | null;
  status: string;
  order: number;
  dependencies: string | null;
  model_used: string | null;
  tools_used: string | null;
  timeout: number | null;
  result: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface TaskStepProgress {
  current: string | null;
  completed: number;
  failed: number;
  skipped: number;
  recoveries: number;
  verifying: boolean;
}

export interface TaskEvent {
  id: string;
  task_id: string;
  step_id: string | null;
  event_type: string;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface TaskCreate {
  title: string;
  description?: string;
  project_id?: string;
  mode?: ExecutionMode;
  definition_of_done?: Record<string, unknown>;
}

export interface TaskListResponse {
  tasks: Task[];
  total: number;
  page: number;
  page_size: number;
}

// Tool types
export interface Tool {
  id: string;
  name: string;
  description: string;
  category: string;
  permissions: string[];
  risk_level: string;
  available: boolean;
  disabled_reason: string | null;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
}

// Model types
export interface Model {
  id: string;
  name: string;
  provider: string;
  category: string;
  context_window: number;
  max_output_tokens: number;
  supports_streaming: boolean;
  supports_vision: boolean;
  quality_rating: number;
  latency_estimate_ms: number;
  cost_per_1k_tokens: number;
  available: boolean;
}

// Audit types (GET /api/v1/audit — admin only)
export interface TaskArtifact {
  index: number;
  artifact_id: string | null;
  step_id: string | null;
  step_title: string | null;
  filename: string;
  kind: string;
  exists: boolean;
  size: number | null;
  content_type?: string | null;
  hash?: string | null;
  created_at?: string | null;
  run_id?: string | null;
  is_current?: boolean;
  superseded_at?: string | null;
}

// One execution run in the persisted artifact history.
// First-Class Execution Runs: run boundaries come from the durable
// execution_runs table (authoritative), not timestamp inference.
export interface ArtifactHistoryRun {
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
}

export interface AuditEvent {
  event_id: string;
  actor_id: string;
  action: string;
  resource: string | null;
  allowed: boolean;
  details: Record<string, unknown> | null;
  timestamp: string;
}

// UI State types
export interface TaskStreamEvent {
  id: string;
  task_id: string;
  step_id: string | null;
  event_type: string;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface CommandState {
  input: string;
  isProcessing: boolean;
  error: string | null;
}
