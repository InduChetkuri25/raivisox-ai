'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useAuth } from '@/components/providers';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { artifactsApi, tasksApi } from '@/lib/api';
import { deriveStepProgress, formatDate, formatRelativeTime, getStatusColor, getStatusLabel } from '@/lib/utils';
import type { ArtifactHistoryRun, Task, TaskArtifact } from '@/lib/types';

const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled', 'partially_completed'];

export default function TaskDetailPage() {
  const params = useParams<{ id: string }>();
  const taskId = params?.id;
  const { token } = useAuth();

  const [task, setTask] = useState<Task | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const [notFound, setNotFound] = useState(false);
  const [showRawDod, setShowRawDod] = useState(false);

  const load = useCallback(async () => {
    if (!token || !taskId) return;
    try {
      const data = await tasksApi.get(token, taskId);
      setTask(data);
      setNotFound(false);
      setError('');
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to load task';
      if (msg.toLowerCase().includes('not found')) {
        setNotFound(true);
      }
      setError(msg);
    } finally {
      setIsLoading(false);
    }
  }, [token, taskId]);

  useEffect(() => {
    load();
  }, [load]);

  // Poll while the task can still change (execution / recovery in flight).
  // All state is persisted on the task row by the backend, so a refresh —
  // or a second tab — sees exactly the same progress.
  const isTerminal = task ? TERMINAL_STATUSES.includes(task.status) : false;
  useEffect(() => {
    if (!token || !taskId || isTerminal) return;
    const interval = setInterval(load, 2500);
    return () => clearInterval(interval);
  }, [token, taskId, isTerminal, load]);

  const [isExecuting, setIsExecuting] = useState(false);
  const [artifacts, setArtifacts] = useState<TaskArtifact[]>([]);
  const [artifactsError, setArtifactsError] = useState('');
  const [downloading, setDownloading] = useState<number | null>(null);
  const [downloadingBundle, setDownloadingBundle] = useState(false);
  const [preview, setPreview] = useState<{ index: number; filename: string; body: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState<number | null>(null);
  const [previewError, setPreviewError] = useState('');
  // Run history: the persisted, per-run artifact view (Run-Scoped Artifact
  // History). Selecting a run shows exactly that run's registered rows.
  const [history, setHistory] = useState<ArtifactHistoryRun[]>([]);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState('');

  const loadArtifacts = useCallback(async () => {
    if (!token || !taskId) return;
    try {
      // When a specific run is selected, its listing REPLACES the current
      // view (the endpoint guarantees a foreign/unknown run is a 404).
      const data = await artifactsApi.list(token, taskId, selectedRun ?? undefined);
      setArtifacts(data.artifacts || []);
      setArtifactsError('');
    } catch (err) {
      if (selectedRun) {
        setArtifactsError(err instanceof Error ? err.message : 'Failed to load run artifacts');
        setArtifacts([]);
      } else {
        // A missing list (e.g. very old task) is not fatal — show empty.
        setArtifacts([]);
      }
    }
  }, [token, taskId, selectedRun]);

  const loadHistory = useCallback(async () => {
    if (!token || !taskId) return;
    try {
      const data = await artifactsApi.history(token, taskId);
      setHistory(data.runs || []);
      setHistoryError('');
    } catch {
      // History is supplementary — never fatal.
      setHistory([]);
    }
  }, [token, taskId]);

  useEffect(() => {
    loadArtifacts();
  }, [loadArtifacts]);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  // Refresh artifacts + history whenever the task reaches a terminal state
  // (the pipeline has then written everything it is going to write).
  useEffect(() => {
    if (isTerminal) {
      loadArtifacts();
      loadHistory();
    }
  }, [isTerminal, loadArtifacts, loadHistory]);

  const isPreviewable = (filename: string) =>
    /\.(py|txt|md|json|csv|log|yml|yaml|toml|ini|cfg|html?|xml|js|ts|sh|bat|rs|go|java|c|cpp|h)$/i.test(filename);

  const handlePreview = async (index: number, filename: string) => {
    if (!token || !taskId || previewLoading !== null) return; // no duplicate requests
    setPreviewLoading(index);
    setPreviewError('');
    try {
      const resp = await artifactsApi.download(token, taskId, index);
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || `Preview failed (${resp.status})`);
      }
      const text = await resp.text();
      setPreview({ index, filename, body: text });
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : 'Preview failed');
    } finally {
      setPreviewLoading(null);
    }
  };

  const handleDownloadBundle = async () => {
    if (!token || !taskId || downloadingBundle) return; // no duplicate requests
    setDownloadingBundle(true);
    setArtifactsError('');
    try {
      const resp = await artifactsApi.downloadBundle(token, taskId);
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || `Bundle download failed (${resp.status})`);
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${taskId}-artifacts.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setArtifactsError(err instanceof Error ? err.message : 'Bundle download failed');
    } finally {
      setDownloadingBundle(false);
    }
  };

  const handleDownload = async (index: number, filename: string) => {
    if (!token || !taskId) return;
    setDownloading(index);
    try {
      const resp = await artifactsApi.download(token, taskId, index);
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || `Download failed (${resp.status})`);
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setArtifactsError(err instanceof Error ? err.message : 'Download failed');
    } finally {
      setDownloading(null);
    }
  };

  // Download by stable registry ID — resolves historical/superseded rows.
  // `currentOnly=false` (superseded row) is metadata-only by design: the
  // physical file's bytes were replaced by a later run, so there is nothing
  // honest to download; the button surfaces that instead of faking bytes.
  const handleDownloadById = async (artifactId: string, filename: string, currentOnly: boolean) => {
    if (!token || !taskId || downloading !== null) return;
    if (!currentOnly) {
      setPreviewError('Superseded artifact — metadata only (bytes were replaced by a later run).');
      return;
    }
    setDownloading(-1);
    setArtifactsError('');
    try {
      const resp = await artifactsApi.downloadById(token, taskId, artifactId);
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || `Download failed (${resp.status})`);
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setArtifactsError(err instanceof Error ? err.message : 'Download failed');
    } finally {
      setDownloading(null);
    }
  };

  const handleExecute = async () => {
    if (!token || !taskId) return;
    setIsExecuting(true);
    setError('');
    try {
      await tasksApi.execute(token, taskId, { async_mode: true });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to execute task');
    } finally {
      setIsExecuting(false);
    }
  };

  if (isLoading) {
    return (
      <div className="min-h-screen bg-dark-900 flex items-center justify-center">
        <div className="text-center">
          <div className="w-12 h-12 border-4 border-raivisox-500 border-t-transparent rounded-full animate-spin mx-auto mb-4" />
          <p className="text-muted-foreground">Loading task…</p>
        </div>
      </div>
    );
  }

  if (notFound || (!task && error)) {
    return (
      <div className="min-h-screen bg-dark-900 flex items-center justify-center">
        <div className="text-center max-w-md p-8">
          <div className="text-5xl mb-4">{notFound ? '🔍' : '⚠️'}</div>
          <h1 className="text-xl font-bold mb-2">{notFound ? 'Task not found' : 'Failed to load task'}</h1>
          <p className="text-muted-foreground text-sm mb-6">
            {notFound
              ? 'This task does not exist or belongs to another account.'
              : error}
          </p>
          <Link href="/">
            <Button variant="outline">Back to dashboard</Button>
          </Link>
        </div>
      </div>
    );
  }

  if (!task) return null;

  const result = (task.result ?? {}) as Record<string, unknown>;
  const verification = result.verification as Record<string, unknown> | undefined;
  const dod = (result.definition_of_done ?? task.definition_of_done) as
    | Record<string, unknown>
    | undefined;
  const recovery = result.recovery as Record<string, unknown> | null | undefined;
  const events = Array.isArray(task.events) ? task.events : [];
  const steps = Array.isArray(task.steps) ? task.steps : [];

  // Live progress derived from REAL persisted transition events only —
  // shared with the dashboard via deriveStepProgress (single source of
  // truth; never fabricated).
  const stepEvents = events.filter((e) =>
    ['step_started', 'step_completed', 'step_failed', 'step_skipped'].includes(e.event_type)
  );
  const progress = deriveStepProgress(task.events);
  const currentStepTitle = progress?.current ?? null;
  const completedCount = progress?.completed ?? 0;
  const failedCount = progress?.failed ?? 0;
  const skippedCount = progress?.skipped ?? 0;
  const recoveryCount = progress?.recoveries ?? 0;
  const verifyingNow = progress?.verifying ?? false;

  return (
    <div className="min-h-screen bg-dark-900">
      <header className="border-b border-dark-700 bg-dark-800/50 backdrop-blur sticky top-0 z-50">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <Link href="/" className="flex items-center gap-3 group">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-raivisox-500 to-raivisox-700 flex items-center justify-center">
              <span className="text-white font-bold">R</span>
            </div>
            <span className="font-semibold gradient-text group-hover:text-raivisox-300">RAIVISOX AI</span>
          </Link>
          <Link href="/" className="text-sm text-muted-foreground hover:text-white">
            ← Dashboard
          </Link>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        {/* Header */}
        <div className="p-6 rounded-xl bg-dark-800 border border-dark-700">
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <Badge
              variant={
                task.status === 'completed' ? 'success'
                : task.status === 'failed' ? 'destructive'
                : task.status === 'executing' ? 'warning'
                : 'outline'
              }
            >
              {getStatusLabel(task.status)}
            </Badge>
            {task.outcome && (
              <Badge
                variant={
                  task.outcome === 'verified' ? 'success'
                  : task.outcome === 'failed' ? 'destructive'
                  : 'warning'
                }
              >
                {task.outcome === 'verified' ? '✓ Verified'
                  : task.outcome === 'failed' ? '✗ Failed'
                  : 'Completed (unverified)'}
              </Badge>
            )}
            {!isTerminal && (
              <span className="text-xs text-blue-400 animate-pulse">live — updating…</span>
            )}
            {!isTerminal && (
              <Button size="sm" variant="outline" onClick={handleExecute} disabled={isExecuting}>
                {isExecuting ? 'Starting…' : 'Run / Re-run'}
              </Button>
            )}
          </div>
          <h1 className="text-2xl font-bold mb-1">{task.title}</h1>
          {task.goal && <p className="text-muted-foreground mb-4">{task.goal}</p>}
          <dl className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
            <div>
              <dt className="text-muted-foreground text-xs">Task ID</dt>
              <dd className="font-mono text-xs break-all">{task.id}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Created</dt>
              <dd>{formatDate(task.created_at)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Updated</dt>
              <dd title={formatDate(task.updated_at)}>{formatRelativeTime(task.updated_at)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Mode</dt>
              <dd className="text-xs break-words">{String(result.mode ?? task.mode)}</dd>
            </div>
          </dl>
        </div>

        {/* Verification & outcome */}
        <div className="grid md:grid-cols-3 gap-4">
          <div className="p-5 rounded-xl bg-dark-800 border border-dark-700">
            <h2 className="text-sm font-semibold text-muted-foreground mb-2">Verification</h2>
            {verification ? (
              <>
                <p className={`text-lg font-bold ${verification.verdict === 'passed' ? 'text-green-400' : 'text-red-400'}`}>
                  {String(verification.verdict ?? 'n/a')}
                </p>
                <p className="text-xs text-muted-foreground">
                  Confidence: {typeof verification.confidence === 'number'
                    ? `${Math.round(verification.confidence * 100)}%`
                    : 'n/a'}
                </p>
                {Array.isArray(verification.failed_checks) && verification.failed_checks.length > 0 && (
                  <ul className="mt-2 text-xs text-red-400 list-disc list-inside">
                    {(verification.failed_checks as string[]).map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <p className="text-sm text-muted-foreground">No verification yet</p>
            )}
          </div>

          <div className="p-5 rounded-xl bg-dark-800 border border-dark-700">
            <h2 className="text-sm font-semibold text-muted-foreground mb-2">Definition of Done</h2>
            {dod ? (
              <>
                <p className={`text-lg font-bold ${
                  dod.satisfied === true ? 'text-green-400'
                  : dod.satisfied === false ? 'text-red-400'
                  : 'text-yellow-400'
                }`}>
                  {dod.satisfied === true ? 'Satisfied'
                    : dod.satisfied === false ? 'Not satisfied'
                    : dod.applicable ? 'Unknown' : 'Not applicable'}
                </p>
                <p className="text-xs text-muted-foreground">
                  {Array.isArray(dod.criteria) ? `${dod.criteria.length} criteria · ` : ''}
                  {dod.status != null ? String(dod.status) : ''}
                </p>
                <button
                  type="button"
                  className="text-xs text-raivisox-400 hover:text-raivisox-300 mt-2"
                  onClick={() => setShowRawDod((v) => !v)}
                >
                  {showRawDod ? 'Hide raw JSON ▲' : 'Show raw JSON ▼'}
                </button>
                {showRawDod && (
                  <pre className="mt-2 text-[10px] leading-relaxed bg-dark-900 rounded-lg p-2 overflow-auto max-h-48 border border-dark-700">
                    {JSON.stringify(dod, null, 2)}
                  </pre>
                )}
              </>
            ) : (
              <p className="text-sm text-muted-foreground">No DoD derived for this task</p>
            )}
          </div>

          <div className="p-5 rounded-xl bg-dark-800 border border-dark-700">
            <h2 className="text-sm font-semibold text-muted-foreground mb-2">Recovery</h2>
            <p className={`text-lg font-bold ${
              Number(result.recovery_attempts) > 0 ? 'text-orange-400' : 'text-muted-foreground'
            }`}>
              {String(result.recovery_attempts ?? 0)} attempt(s)
            </p>
            {recovery && (
              <p className="text-xs text-muted-foreground mt-1">
                Strategy: {String(recovery.strategy ?? 'n/a')} · Outcome: {String(recovery.outcome ?? 'n/a')}
              </p>
            )}
            {typeof result.simulated_steps === 'number' && (
              <p className="text-xs text-muted-foreground mt-2">
                {result.simulated_steps} simulated step(s) in pipeline
              </p>
            )}
          </div>
        </div>

        {/* Final result */}
        {result.message ? (
          <div className="p-5 rounded-xl bg-dark-800 border border-dark-700">
            <h2 className="text-sm font-semibold text-muted-foreground mb-2">Result</h2>
            <p className="text-sm">{String(result.message)}</p>
            {task.error && (
              <p className="text-sm text-red-400 mt-2">
                Error: {String((task.error as Record<string, unknown>).message ?? 'task failed')}
              </p>
            )}
          </div>
        ) : null}

        {/* Live progress — derived ONLY from persisted transition events */}
        {stepEvents.length > 0 && (
          <div className="p-5 rounded-xl bg-dark-800 border border-dark-700">
            <h2 className="text-sm font-semibold text-muted-foreground mb-3">
              Live progress
              {!isTerminal && <span className="ml-2 text-blue-400 animate-pulse">●</span>}
            </h2>
            {currentStepTitle ? (
              <p className="text-sm mb-3">
                <span className="text-blue-400 font-mono text-xs mr-2">▶ running</span>
                {currentStepTitle}
              </p>
            ) : (
              <p className="text-sm mb-3 text-muted-foreground">
                {isTerminal ? 'No step currently running.' : 'Waiting for next step…'}
              </p>
            )}
            <div className="flex flex-wrap gap-4 text-xs">
              <span className="text-green-400">✓ {completedCount} completed</span>
              {failedCount > 0 && <span className="text-red-400">✗ {failedCount} failed</span>}
              {skippedCount > 0 && <span className="text-gray-400">⤼ {skippedCount} skipped</span>}
              {recoveryCount > 0 && <span className="text-amber-400">↻ {recoveryCount} recovery attempt(s)</span>}
              {verifyingNow && <span className="text-raivisox-400 animate-pulse">⧗ verifying…</span>}
            </div>
          </div>
        )}

        {/* Artifacts — real files the pipeline wrote */}
        <div className="p-5 rounded-xl bg-dark-800 border border-dark-700">
          <div className="flex items-center justify-between gap-3 mb-3">
            <h2 className="text-sm font-semibold text-muted-foreground">
              Artifacts ({artifacts.length})
              {selectedRun && ' — selected run'}
            </h2>
            {artifacts.length > 0 && !selectedRun && (
              <Button
                size="sm"
                variant="secondary"
                disabled={downloadingBundle || !artifacts.some((a) => a.exists)}
                onClick={handleDownloadBundle}
              >
                {downloadingBundle ? 'Packing…' : 'Download all (.zip)'}
              </Button>
            )}
          </div>
          {history.length > 1 && (
            <div className="flex flex-wrap items-center gap-2 mb-3">
              <button
                className={`text-xs px-2 py-1 rounded-md border ${selectedRun === null ? 'border-raivisox-500 bg-raivisox-500/10 text-raivisox-300' : 'border-dark-700 text-muted-foreground hover:text-foreground'}`}
                onClick={() => setSelectedRun(null)}
              >
                Current view
              </button>
              {history.map((r, i) => {
                // First-Class Execution Runs: chips show the DURABLE run row's
                // status/outcome and real start→end times. Legacy/unscoped
                // groups (pre-execution_runs rows) are labeled as such —
                // never dressed up as a real run.
                const started = r.started_at ? new Date(r.started_at) : null;
                const finished = r.finished_at ? new Date(r.finished_at) : null;
                const timeLabel = started
                  ? `${started.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}` +
                    (finished
                      ? ` → ${finished.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
                      : ' → …')
                  : 'legacy';
                return (
                  <button
                    key={r.run_id}
                    title={
                      r.is_legacy_run
                        ? `Legacy artifact group (recorded before execution runs existed)${r.started_at ? ` · first artifact ${started?.toLocaleString()}` : ''}`
                        : `Run ${r.run_id} · started ${started?.toLocaleString() ?? 'unknown'}${finished ? ` · finished ${finished.toLocaleString()}` : ' · unfinished'}${r.outcome ? ` · outcome ${r.outcome}` : ''}`
                    }
                    className={`text-xs px-2 py-1 rounded-md border font-mono ${selectedRun === r.run_id ? 'border-raivisox-500 bg-raivisox-500/10 text-raivisox-300' : 'border-dark-700 text-muted-foreground hover:text-foreground'}${r.is_legacy_run ? ' opacity-70' : ''}`}
                    onClick={() => setSelectedRun(r.run_id)}
                  >
                    {r.is_legacy_run ? 'legacy' : `run ${i + 1}`} · {r.artifact_count} file{r.artifact_count === 1 ? '' : 's'}
                    {r.status && ` · ${r.status}`}
                    {r.outcome && r.outcome !== r.status && ` (${r.outcome})`}
                    {r.is_legacy_run ? '' : ` · ${timeLabel}`}
                  </button>
                );
              })}
            </div>
          )}
          {historyError && <p className="text-xs text-red-400 mb-2">{historyError}</p>}
          {artifactsError && <p className="text-xs text-red-400 mb-2">{artifactsError}</p>}
          {previewError && <p className="text-xs text-red-400 mb-2">{previewError}</p>}
          {artifacts.length === 0 ? (
            <p className="text-sm text-muted-foreground">No files were produced by this task.</p>
          ) : (
            <ul className="space-y-2">
              {artifacts.map((a) => (
                <li key={a.index} className="flex items-center justify-between gap-3 text-sm">
                  <div className="min-w-0">
                    <span className="font-mono text-xs text-raivisox-300">{a.filename}</span>
                    <span className="text-muted-foreground text-xs ml-2">
                      {a.kind} · step: {a.step_title}
                      {a.size !== null && ` · ${(a.size / 1024).toFixed(1)} KB`}
                      {!a.exists && ' · missing on disk'}
                    </span>
                    {a.hash && (
                      <span
                        className="block text-[10px] text-muted-foreground/70 font-mono mt-0.5"
                        title={`sha256 ${a.hash}${a.created_at ? ` · registered ${a.created_at}` : ''}`}
                      >
                        sha256:{a.hash.slice(0, 12)}…
                        {a.created_at && ` · ${new Date(a.created_at).toLocaleString()}`}
                      </span>
                    )}
                    {selectedRun && a.is_current === false && (
                      <span className="block text-[10px] text-amber-400/80 mt-0.5">
                        superseded within run · metadata only
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {isPreviewable(a.filename) && (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={!a.exists || previewLoading === a.index}
                        onClick={() => handlePreview(a.index, a.filename)}
                      >
                        {previewLoading === a.index ? 'Loading…' : 'Preview'}
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!a.artifact_id && !a.exists}
                      onClick={() =>
                        a.artifact_id
                          ? handleDownloadById(a.artifact_id, a.filename, a.is_current !== false)
                        : handleDownload(a.index, a.filename)
                      }
                    >
                      {downloading === a.index ? 'Downloading…' : a.is_current === false ? 'Metadata' : 'Download'}
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {preview && (
            <div className="mt-4 rounded-lg border border-dark-700 bg-dark-900 overflow-hidden">
              <div className="flex items-center justify-between px-3 py-2 border-b border-dark-700">
                <span className="font-mono text-xs text-raivisox-300">
                  {preview.filename}
                  {preview.body.length === 0 && ' · (empty file)'}
                </span>
                <button
                  className="text-xs text-muted-foreground hover:text-foreground"
                  onClick={() => setPreview(null)}
                >
                  Close
                </button>
              </div>
              {/* Plain text in a <pre> — React escapes it; artifacts are never rendered as HTML. */}
              <pre className="max-h-80 overflow-auto p-3 text-xs leading-relaxed whitespace-pre-wrap">
                {preview.body || '(empty file)'}</pre>
            </div>
          )}
        </div>

        {/* Pipeline steps */}
        <div className="p-5 rounded-xl bg-dark-800 border border-dark-700">
          <h2 className="text-sm font-semibold text-muted-foreground mb-3">
            Pipeline steps ({steps.length})
          </h2>
          {steps.length === 0 ? (
            <p className="text-sm text-muted-foreground">Task has not been executed yet.</p>
          ) : (
            <ul className="space-y-2">
              {steps.map((s) => (
                <li key={s.id} className="flex items-start gap-3 text-sm">
                  <span className={`mt-1.5 inline-block w-2 h-2 rounded-full shrink-0 ${
                    s.status === 'completed' ? 'bg-green-500'
                    : s.status === 'failed' ? 'bg-red-500'
                    : s.status === 'cancelled' || s.status === 'skipped' ? 'bg-gray-500'
                    : 'bg-blue-500 animate-pulse'
                  }`} />
                  <div className="min-w-0">
                    <span>{s.title}</span>
                    <span className="text-muted-foreground text-xs ml-2">({s.status})</span>
                    {s.error && (
                      <span className="block text-xs text-red-400">
                        {String((s.error as Record<string, unknown>).message ?? '')}
                      </span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Execution events */}
        <div className="p-5 rounded-xl bg-dark-800 border border-dark-700">
          <h2 className="text-sm font-semibold text-muted-foreground mb-3">
            Execution events ({events.length})
          </h2>
          {events.length === 0 ? (
            <p className="text-sm text-muted-foreground">No events recorded.</p>
          ) : (
            <ul className="space-y-3">
              {events.map((e) => (
                <li key={e.id} className="text-sm border-l-2 border-dark-600 pl-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-raivisox-400">{e.event_type}</span>
                    <span className="text-xs text-muted-foreground">{formatDate(e.created_at)}</span>
                  </div>
                  {e.payload && (
                    <pre className="mt-1 text-[10px] text-muted-foreground bg-dark-900 rounded p-2 overflow-auto max-h-32 border border-dark-700">
                      {JSON.stringify(e.payload, null, 2)}
                    </pre>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </main>
    </div>
  );
}
