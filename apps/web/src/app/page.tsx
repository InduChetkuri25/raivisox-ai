'use client';

import { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { useAuth } from '@/components/providers';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { tasksApi } from '@/lib/api';
import { deriveStepProgress, getStatusColor, getStatusLabel, formatRelativeTime, truncateText } from '@/lib/utils';
import type { Task } from '@/lib/types';

export default function Home() {
  const { user, token, login, register, logout, isLoading } = useAuth();
  const [showAuth, setShowAuth] = useState(false);
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [authError, setAuthError] = useState('');

  const [tasks, setTasks] = useState<Task[]>([]);
  const [isLoadingTasks, setIsLoadingTasks] = useState(false);
  const [newTaskTitle, setNewTaskTitle] = useState('');
  const [isCreatingTask, setIsCreatingTask] = useState(false);
  const [taskError, setTaskError] = useState('');
  const [executingId, setExecutingId] = useState<string | null>(null);
  const [expandedTaskId, setExpandedTaskId] = useState<string | null>(null);

  const handleAuth = async (e: React.FormEvent) => {
    e.preventDefault();
    setAuthError('');

    try {
      if (authMode === 'login') {
        await login(email, password);
      } else {
        // register() auto-logs-in on success and persists the session.
        await register(email, password, fullName || undefined);
      }
      setShowAuth(false);
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : 'Authentication failed');
    }
  };

  const fetchTasks = useCallback(async () => {
    if (!token) return;
    setIsLoadingTasks(true);
    try {
      const data = await tasksApi.list(token, undefined, undefined, 1, 20);
      setTasks(data.tasks);
    } catch (err) {
      console.error('Failed to fetch tasks:', err);
    } finally {
      setIsLoadingTasks(false);
    }
  }, [token]);

  // Adaptive polling: 4s while any task is non-terminal (near-live
  // progress from real persisted state), 30s when idle (slow catch-up so
  // tasks created in another tab/session still appear — the list itself
  // can change even when every visible task is terminal).
  const hasBusyTask = tasks.some(
    (t) => !['completed', 'failed', 'cancelled', 'partially_completed'].includes(t.status)
  );
  useEffect(() => {
    if (!token) return;
    fetchTasks();
    const interval = setInterval(fetchTasks, hasBusyTask ? 4000 : 30000);
    return () => clearInterval(interval);
  }, [token, fetchTasks, hasBusyTask]);

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTaskTitle.trim() || !token) return;

    setIsCreatingTask(true);
    setTaskError('');
    try {
      // The command-center input IS the intent; send it as the task goal so
      // the pipeline (intent → decompose → DoD) has the real objective.
      const task = await tasksApi.create(token, truncateText(newTaskTitle.trim(), 80), newTaskTitle.trim());
      setTasks((prev) => [task, ...prev]);
      setNewTaskTitle('');
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to create task';
      setTaskError(msg);
      console.error('Failed to create task:', err);
    } finally {
      setIsCreatingTask(false);
    }
  };

  const handleExecuteTask = async (taskId: string) => {
    if (!token) return;
    setExecutingId(taskId);
    setTaskError('');
    try {
      const res = await tasksApi.execute(token, taskId, { async_mode: true });
      // The backend accepts the request (202) and runs the pipeline in the
      // background; state is persisted on the task row, which the 10s poll
      // (and any other tab) observes. Refresh immediately for the flip.
      if (res && typeof res === 'object' && 'status' in res) {
        setTasks((prev) =>
          prev.map((t) => (t.id === taskId ? { ...t, status: 'executing' } : t)),
        );
      }
      // The adaptive poller (hasBusyTask) takes over from here: it polls
      // every 4s while the task runs and stops at the terminal state.
      await fetchTasks();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to execute task';
      setTaskError(msg);
      console.error('Failed to execute task:', err);
      fetchTasks();
    } finally {
      setExecutingId(null);
    }
  };

  if (isLoading) {
    return (
      <div className="min-h-screen bg-dark-900 flex items-center justify-center">
        <div className="text-center">
          <div className="w-12 h-12 border-4 border-raivisox-500 border-t-transparent rounded-full animate-spin mx-auto mb-4" />
          <p className="text-muted-foreground">Loading RAIVISOX...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-dark-900">
      {/* Header */}
      <header className="border-b border-dark-700 bg-dark-800/50 backdrop-blur sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-raivisox-500 to-raivisox-700 flex items-center justify-center">
                <span className="text-white font-bold text-lg">R</span>
              </div>
              <div>
                <h1 className="text-lg font-semibold gradient-text">RAIVISOX AI</h1>
                <p className="text-xs text-muted-foreground -mt-0.5">From Intent to Verified Outcome</p>
              </div>
            </div>

            <div className="flex items-center gap-4">
              {user ? (
                <div className="flex items-center gap-4">
                  <span className="text-sm text-muted-foreground hidden sm:block">{user.email}</span>
                  <Button variant="ghost" size="sm" onClick={logout}>
                    Sign Out
                  </Button>
                </div>
              ) : (
                <Button variant="outline" size="sm" onClick={() => setShowAuth(true)}>
                  Sign In
                </Button>
              )}
            </div>
          </div>
          <div className="flex items-center gap-4">
            <Link href="/projects" className="text-sm text-muted-foreground hover:text-white transition-colors">
              Projects
            </Link>
            <Link href="/audit" className="text-sm text-muted-foreground hover:text-white transition-colors">
              Audit
            </Link>
          </div>
        </div>
      </header>

      {/* Hero Section */}
      <section className="relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-b from-raivisox-500/10 via-transparent to-transparent" />
        <div className="absolute top-20 left-1/4 w-96 h-96 bg-raivisox-500/20 rounded-full blur-3xl" />
        <div className="absolute top-40 right-1/4 w-64 h-64 bg-purple-500/20 rounded-full blur-3xl" />

        <div className="relative max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pt-24 pb-16">
          <div className="text-center max-w-4xl mx-auto">
            <Badge variant="default" className="mb-6 px-4 py-1.5 text-sm">
              Next-Gen Adaptive Digital Intelligence
            </Badge>
            <h2 className="text-4xl sm:text-5xl lg:text-6xl font-bold tracking-tight mb-6">
              <span className="gradient-text">From Intent</span> to{' '}
              <span className="text-white">Verified Outcome</span>
            </h2>
            <p className="text-lg text-muted-foreground max-w-2xl mx-auto mb-8">
              RAIVISOX doesn&apos;t just generate answers. It executes real work, verifies the results,
              recovers from failures, and delivers proven outcomes.
            </p>

            {!user && (
              <div className="flex justify-center gap-4">
                <Button size="lg" onClick={() => setShowAuth(true)}>
                  Get Started
                </Button>
                <Button size="lg" variant="outline" onClick={() => {
                  // Scroll to features
                  document.getElementById('features')?.scrollIntoView({ behavior: 'smooth' });
                }}>
                  Learn More
                </Button>
              </div>
            )}
          </div>
        </div>
      </section>

      {/* Features Section */}
      <section id="features" className="py-20">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center mb-16">
            <h3 className="text-3xl font-bold mb-4">Why RAIVISOX?</h3>
            <p className="text-muted-foreground max-w-2xl mx-auto">
              Most AI assistants generate answers. RAIVISOX executes real outcomes with verification.
            </p>
          </div>

          <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-8">
            {[
              {
                icon: '🎯',
                title: 'Adaptive Execution',
                description: 'Dynamically determines the best execution strategy based on success probability, speed, cost, reliability, and risk.',
              },
              {
                icon: '✅',
                title: 'Verification First',
                description: 'Never claims success without evidence. Every major task produces verification evidence.',
              },
              {
                icon: '🔄',
                title: 'Intelligent Recovery',
                description: 'Diagnoses failures, generates alternatives, and recovers intelligently instead of blindly retrying.',
              },
              {
                icon: '🔌',
                title: 'Model Agnostic',
                description: 'Works with multiple AI providers. The foundation models provide intelligence; RAIVISOX provides orchestration.',
              },
              {
                icon: '🛡️',
                title: 'Security Kernel',
                description: 'Every action passes through security controls with permissions, risk assessment, and audit logging.',
              },
              {
                icon: '📊',
                title: 'Observable Execution',
                description: 'Complete execution timeline, metrics, and traces for every task. Know exactly what happened.',
              },
            ].map((feature, i) => (
              <div
                key={i}
                className="p-6 rounded-xl bg-dark-800 border border-dark-700 hover:border-raivisox-500/50 transition-all duration-300 group"
              >
                <div className="text-3xl mb-4">{feature.icon}</div>
                <h4 className="font-semibold mb-2 group-hover:text-raivisox-400 transition-colors">
                  {feature.title}
                </h4>
                <p className="text-sm text-muted-foreground">{feature.description}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* User Dashboard */}
      {user && (
        <section className="py-20 border-t border-dark-700">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div className="flex flex-col lg:flex-row gap-8">
              {/* Command Center */}
              <div className="lg:w-2/3">
                <div className="mb-8">
                  <h3 className="text-2xl font-bold mb-2">Command Center</h3>
                  <p className="text-muted-foreground">
                    Describe what you want to accomplish, and RAIVISOX will handle the rest.
                  </p>
                </div>

                {taskError && (
                  <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/40 text-sm text-red-400">
                    {taskError}
                  </div>
                )}

                <form onSubmit={handleCreateTask} className="mb-8">
                  <div className="flex gap-3">
                    <Input
                      type="text"
                      placeholder="e.g., Build a clinic appointment application..."
                      value={newTaskTitle}
                      onChange={(e) => setNewTaskTitle(e.target.value)}
                      className="flex-1 bg-dark-800 border-dark-600 focus:border-raivisox-500"
                      disabled={isCreatingTask || !user}
                    />
                    <Button
                      type="submit"
                      disabled={!newTaskTitle.trim() || isCreatingTask}
                      className="bg-raivisox-600 hover:bg-raivisox-700"
                    >
                      {isCreatingTask ? (
                        <div className="w-5 h-5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                      ) : (
                        'Create Task'
                      )}
                    </Button>
                  </div>
                </form>

                {/* Task List */}
                <div>
                  <div className="flex items-center justify-between mb-4">
                    <h4 className="font-semibold">Recent Tasks</h4>
                    {tasks.length > 0 && (
                      <span className="text-sm text-muted-foreground">
                        {tasks.length} task{tasks.length !== 1 ? 's' : ''}
                      </span>
                    )}
                  </div>

                  {isLoadingTasks ? (
                    <div className="space-y-3">
                      {[1, 2, 3].map((i) => (
                        <div key={i} className="h-16 bg-dark-800 rounded-lg animate-pulse" />
                      ))}
                    </div>
                  ) : tasks.length === 0 ? (
                    <div className="text-center py-12 bg-dark-800 rounded-xl border border-dashed border-dark-600">
                      <div className="text-4xl mb-4">🎯</div>
                      <p className="text-muted-foreground mb-2">No tasks yet</p>
                      <p className="text-sm text-muted-foreground">
                        Use the command center above to create your first task
                      </p>
                    </div>
                  ) : (
                    <div className="space-y-3">
                      {tasks.map((task, index) => (
                        <div
                          key={task.id}
                          className={`p-4 rounded-xl bg-dark-800 border transition-all duration-300 animate-fade-in ${
                            index === 0 ? 'border-raivisox-500/50' : 'border-dark-700'
                          }`}
                          style={{ animationDelay: `${index * 50}ms` }}
                        >
                          <div className="flex items-start justify-between gap-4">
                            <Link href={`/tasks/${task.id}`} className="flex-1 min-w-0 group/card">
                              <div className="flex items-center gap-2 mb-1">
                                <span
                                  aria-hidden
                                  className={`inline-block w-2.5 h-2.5 rounded-full ${getStatusColor(task.status)}`}
                                />
                                <h5 className="font-medium truncate group-hover/card:text-raivisox-300 transition-colors">{truncateText(task.title, 60)}</h5>
                              </div>
                              {task.goal && (
                                <p className="text-sm text-muted-foreground truncate mb-2">
                                  {truncateText(task.goal, 100)}
                                </p>
                              )}
                              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                                <Badge variant={
                                  task.status === 'completed' ? 'success' :
                                  task.status === 'failed' ? 'destructive' :
                                  task.status === 'executing' ? 'warning' : 'outline'
                                }>
                                  {getStatusLabel(task.status)}
                                </Badge>
                                {task.outcome && (
                                  <Badge variant={
                                    task.outcome === 'verified' ? 'success' :
                                    task.outcome === 'failed' ? 'destructive' : 'warning'
                                  }>
                                    {task.outcome === 'verified'
                                      ? '✓ Verified'
                                      : task.outcome === 'failed'
                                        ? '✗ Failed'
                                        : 'Completed (unverified)'}
                                  </Badge>
                                )}
                                <span>{formatRelativeTime(task.created_at)}</span>
                              </div>

                              {/* Live progress — ONLY from persisted real
                                  transition events; shown while the task is
                                  executing/recovering/verifying. */}
                              {(() => {
                                const prog = deriveStepProgress(task.events);
                                if (!prog) return null;
                                const busy = !['completed', 'failed', 'cancelled', 'partially_completed'].includes(task.status);
                                if (!busy && !prog.current) return null;
                                return (
                                  <div className="mt-2 rounded-lg bg-dark-900 border border-dark-700 px-3 py-2 text-xs space-y-1">
                                    <div className="flex flex-wrap items-center gap-3">
                                      {prog.current && (
                                        <span className="text-blue-400">
                                          <span className="animate-pulse mr-1">▶</span>
                                          {truncateText(prog.current, 48)}
                                        </span>
                                      )}
                                      <span className="text-green-400">✓ {prog.completed} done</span>
                                      {prog.failed > 0 && <span className="text-red-400">✗ {prog.failed} failed</span>}
                                      {prog.skipped > 0 && <span className="text-gray-400">⤼ {prog.skipped} skipped</span>}
                                      {prog.recoveries > 0 && <span className="text-amber-400">↻ {prog.recoveries}</span>}
                                      {prog.verifying && <span className="text-raivisox-400 animate-pulse">⧗ verifying…</span>}
                                      {!busy && !prog.current && (
                                        <span className="text-muted-foreground">finished</span>
                                      )}
                                    </div>
                                  </div>
                                );
                              })()}

                              {/* Result details (expandable) */}
                              {task.status === 'completed' || task.status === 'failed' ? (
                                <div className="mt-3">
                                  <button
                                    type="button"
                                    onClick={() =>
                                      setExpandedTaskId(expandedTaskId === task.id ? null : task.id)
                                    }
                                    className="text-xs text-raivisox-400 hover:text-raivisox-300 transition-colors"
                                  >
                                    {expandedTaskId === task.id ? 'Hide details ▲' : 'Show details ▼'}
                                  </button>
                                  {expandedTaskId === task.id && (() => {
                                    const res = (task.result ?? {}) as Record<string, unknown>;
                                    const verification = res.verification as Record<string, unknown> | undefined;
                                    const dod = res.definition_of_done as Record<string, unknown> | undefined;
                                    return (
                                    <div className="mt-2 space-y-2 rounded-lg bg-dark-900 border border-dark-700 p-3">
                                      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                                        {res.steps_total != null && (
                                          <div><span className="text-muted-foreground">Steps:</span> {String(res.steps_total)}</div>
                                        )}
                                        {res.steps_completed != null && (
                                          <div className="text-green-400">Completed: {String(res.steps_completed)}</div>
                                        )}
                                        {res.steps_failed != null && (
                                          <div className="text-red-400">Failed: {String(res.steps_failed)}</div>
                                        )}
                                        {res.recovery_attempts != null && (
                                          <div className="text-orange-400">Recoveries: {String(res.recovery_attempts)}</div>
                                        )}
                                      </div>
                                      {verification && (
                                        <div className="text-xs">
                                          <span className="text-muted-foreground">Verification: </span>
                                          <span className={verification.verdict === 'passed' ? 'text-green-400' : 'text-red-400'}>
                                            {String(verification.verdict ?? 'n/a')}
                                          </span>
                                          {dod?.satisfied != null && (
                                            <span className="ml-2">
                                              <span className="text-muted-foreground">DoD: </span>
                                              <span className={dod.satisfied ? 'text-green-400' : 'text-red-400'}>
                                                {dod.satisfied ? 'satisfied' : 'not satisfied'}
                                              </span>
                                            </span>
                                          )}
                                        </div>
                                      )}
                                      {Array.isArray(task.steps) && task.steps.length > 0 && (
                                        <div className="text-xs">
                                          <div className="text-muted-foreground mb-1">Steps</div>
                                          <ul className="space-y-1">
                                            {task.steps.slice(0, 12).map((s) => (
                                              <li key={s.id} className="flex items-center gap-2">
                                                <span className={
                                                  s.status === 'completed' ? 'text-green-400' :
                                                  s.status === 'failed' ? 'text-red-400' :
                                                  s.status === 'skipped' ? 'text-gray-500' : 'text-blue-400'
                                                }>•</span>
                                                <span className="truncate">{truncateText(s.title, 50)}</span>
                                                <span className="text-muted-foreground shrink-0">({s.status})</span>
                                              </li>
                                            ))}
                                          </ul>
                                        </div>
                                      )}
                                      {task.error && (
                                        <div className="text-xs text-red-400">
                                          Error: {String((task.error as Record<string, unknown>).message ?? 'task failed')}
                                        </div>
                                      )}
                                    </div>
                                    );
                                  })()}
                                </div>
                              ) : null}
                            </Link>

                            <div className="flex gap-2">
                              {task.status !== 'completed' && task.status !== 'failed' && task.status !== 'cancelled' && (
                                <Button
                                  size="sm"
                                  variant="primary"
                                  onClick={() => handleExecuteTask(task.id)}
                                  disabled={!token || executingId === task.id}
                                >
                                  {executingId === task.id ? (
                                    <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                                  ) : (
                                    'Execute'
                                  )}
                                </Button>
                              )}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              {/* Sidebar Stats */}
              <div className="lg:w-1/3">
                <div className="grid grid-cols-2 gap-4">
                  <div className="p-4 rounded-xl bg-dark-800 border border-dark-700">
                    <div className="text-3xl font-bold text-raivisox-400">{tasks.length}</div>
                    <div className="text-sm text-muted-foreground">Total Tasks</div>
                  </div>
                  <div className="p-4 rounded-xl bg-dark-800 border border-dark-700">
                    <div className="text-3xl font-bold text-green-400">
                      {tasks.filter(t => t.status === 'completed').length}
                    </div>
                    <div className="text-sm text-muted-foreground">Completed</div>
                  </div>
                  <div className="p-4 rounded-xl bg-dark-800 border border-dark-700">
                    <div className="text-3xl font-bold text-yellow-400">
                      {tasks.filter(t => t.status === 'executing' || t.status === 'understanding' || t.status === 'planning').length}
                    </div>
                    <div className="text-sm text-muted-foreground">In Progress</div>
                  </div>
                  <div className="p-4 rounded-xl bg-dark-800 border border-dark-700">
                    <div className="text-3xl font-bold text-red-400">
                      {tasks.filter(t => t.status === 'failed').length}
                    </div>
                    <div className="text-sm text-muted-foreground">Failed</div>
                  </div>
                </div>

                {/* Quick Actions */}
                <div className="mt-8 p-6 rounded-xl bg-dark-800 border border-dark-700">
                  <h4 className="font-semibold mb-4">Quick Actions</h4>
                  <div className="space-y-2">
                    <Link href="/projects" className="block">
                      <Button variant="outline" className="w-full justify-start">
                        <span className="mr-2">📁</span> Projects
                      </Button>
                    </Link>
                    <Button variant="outline" className="w-full justify-start" disabled>
                      <span className="mr-2">📊</span> View Dashboard
                    </Button>
                    <Button variant="outline" className="w-full justify-start" disabled>
                      <span className="mr-2">⚙️</span> Settings
                    </Button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>
      )}

      {/* Footer */}
      <footer className="border-t border-dark-700 py-8">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center">
          <p className="text-sm text-muted-foreground">
            RAIVISOX AI - Next-Gen Adaptive Digital Intelligence
          </p>
          <p className="text-xs text-muted-foreground mt-1">
            Built with Next.js, FastAPI, and TypeScript
          </p>
        </div>
      </footer>

      {/* Auth Modal */}
      {showAuth && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-dark-800 rounded-2xl border border-dark-600 w-full max-w-md p-8 shadow-2xl animate-fade-in">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-2xl font-bold">{authMode === 'login' ? 'Sign In' : 'Create Account'}</h2>
              <button
                onClick={() => setShowAuth(false)}
                className="text-muted-foreground hover:text-white transition-colors"
              >
                <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <form onSubmit={handleAuth} className="space-y-4">
              <div>
                <label className="block text-sm font-medium mb-1">Email</label>
                <Input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  required
                  className="bg-dark-900"
                />
              </div>

              {authMode === 'register' && (
                <div>
                  <label className="block text-sm font-medium mb-1">Full Name (optional)</label>
                  <Input
                    type="text"
                    value={fullName}
                    onChange={(e) => setFullName(e.target.value)}
                    placeholder="John Doe"
                    className="bg-dark-900"
                  />
                </div>
              )}

              <div>
                <label className="block text-sm font-medium mb-1">Password</label>
                <Input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Your password"
                  required
                  minLength={8}
                  className="bg-dark-900"
                />
              </div>

              {authError && (
                <p className="text-red-400 text-sm">{authError}</p>
              )}

              <Button
                type="submit"
                className="w-full bg-raivisox-600 hover:bg-raivisox-700"
                disabled={!email || !password || isLoading}
              >
                {isLoading ? (
                  <div className="w-5 h-5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                ) : authMode === 'login' ? (
                  'Sign In'
                ) : (
                  'Create Account'
                )}
              </Button>
            </form>

            <div className="mt-6 text-center">
              <button
                onClick={() => {
                  setAuthMode(authMode === 'login' ? 'register' : 'login');
                  setAuthError('');
                }}
                className="text-sm text-raivisox-400 hover:text-raivisox-300 transition-colors"
              >
                {authMode === 'login' ? "Don't have an account? Sign up" : 'Already have an account? Sign in'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
