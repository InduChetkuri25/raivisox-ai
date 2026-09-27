'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useAuth } from '@/components/providers';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { artifactsApi, projectsApi, tasksApi } from '@/lib/api';
import { formatDate, getStatusLabel } from '@/lib/utils';
import type { Project, Task } from '@/lib/types';

export default function ProjectDetailPage() {
  const params = useParams<{ id: string }>();
  const projectId = params?.id;
  const { token } = useAuth();

  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const [notFound, setNotFound] = useState(false);
  const [bundleLoading, setBundleLoading] = useState(false);
  const [bundleError, setBundleError] = useState('');

  const handleDownloadBundle = async () => {
    if (!token || !projectId || bundleLoading) return; // no duplicate requests
    setBundleLoading(true);
    setBundleError('');
    try {
      const resp = await artifactsApi.downloadProjectBundle(token, projectId);
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.detail || `Bundle download failed (${resp.status})`);
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${projectId}-artifacts.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setBundleError(err instanceof Error ? err.message : 'Bundle download failed');
    } finally {
      setBundleLoading(false);
    }
  };

  const load = useCallback(async () => {
    if (!token || !projectId) return;
    setIsLoading(true);
    try {
      const p = await projectsApi.get(token, projectId);
      setProject(p);
      setNotFound(false);
      setError('');
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to load project';
      if (msg.toLowerCase().includes('not found')) setNotFound(true);
      setError(msg);
    }
    try {
      // Backend supports filtering tasks by project_id.
      const t = await tasksApi.list(token, undefined, projectId, 1, 50);
      setTasks(t.tasks ?? []);
    } catch {
      setTasks([]); // task listing is secondary; project header still renders
    } finally {
      setIsLoading(false);
    }
  }, [token, projectId]);

  useEffect(() => {
    load();
  }, [load]);

  if (isLoading) {
    return (
      <div className="min-h-screen bg-dark-900 flex items-center justify-center">
        <div className="text-center">
          <div className="w-12 h-12 border-4 border-raivisox-500 border-t-transparent rounded-full animate-spin mx-auto mb-4" />
          <p className="text-muted-foreground">Loading project…</p>
        </div>
      </div>
    );
  }

  if (notFound || (!project && error)) {
    return (
      <div className="min-h-screen bg-dark-900 flex items-center justify-center">
        <div className="text-center max-w-md p-8">
          <div className="text-5xl mb-4">{notFound ? '🔍' : '⚠️'}</div>
          <h1 className="text-xl font-bold mb-2">{notFound ? 'Project not found' : 'Failed to load project'}</h1>
          <p className="text-muted-foreground text-sm mb-6">
            {notFound ? 'This project does not exist or belongs to another account.' : error}
          </p>
          <Link href="/projects">
            <Button variant="outline">Back to projects</Button>
          </Link>
        </div>
      </div>
    );
  }

  if (!project) return null;

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
          <Link href="/projects" className="text-sm text-muted-foreground hover:text-white">
            ← Projects
          </Link>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        <div className="p-6 rounded-xl bg-dark-800 border border-dark-700">
          <h1 className="text-2xl font-bold mb-1">{project.name}</h1>
          {project.description && <p className="text-muted-foreground mb-3">{project.description}</p>}
          <dl className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
            <div>
              <dt className="text-muted-foreground text-xs">Status</dt>
              <dd>{project.status}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Created</dt>
              <dd>{formatDate(project.created_at)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Updated</dt>
              <dd>{formatDate(project.updated_at)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Project ID</dt>
              <dd className="font-mono text-xs break-all">{project.id}</dd>
            </div>
          </dl>
        </div>

        <div className="p-5 rounded-xl bg-dark-800 border border-dark-700">
          <div className="flex items-center justify-between gap-3 mb-3">
            <h2 className="text-sm font-semibold text-muted-foreground">
              Tasks in this project ({tasks.length})
            </h2>
            {tasks.length > 0 && (
              <Button
                size="sm"
                variant="secondary"
                disabled={bundleLoading}
                onClick={handleDownloadBundle}
              >
                {bundleLoading ? 'Packing…' : 'Download all artifacts (.zip)'}
              </Button>
            )}
          </div>
          {bundleError && <p className="text-xs text-red-400 mb-2">{bundleError}</p>}
          {tasks.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No tasks assigned. Tasks can be assigned to this project via the API ({' '}
              <code className="text-xs">POST /api/v1/tasks</code> with{' '}
              <code className="text-xs">project_id</code>).
            </p>
          ) : (
            <ul className="space-y-3">
              {tasks.map((t) => (
                <li key={t.id}>
                  <Link
                    href={`/tasks/${t.id}`}
                    className="flex items-center justify-between gap-3 p-3 rounded-lg bg-dark-900 border border-dark-700 hover:border-raivisox-500/50 transition-colors"
                  >
                    <div className="min-w-0">
                      <span className="text-sm font-medium truncate block">{t.title}</span>
                      {t.goal && <span className="text-xs text-muted-foreground truncate block">{t.goal}</span>}
                    </div>
                    <Badge
                      variant={
                        t.status === 'completed' ? 'success'
                        : t.status === 'failed' ? 'destructive'
                        : t.status === 'executing' ? 'warning'
                        : 'outline'
                      }
                    >
                      {getStatusLabel(t.status)}
                    </Badge>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      </main>
    </div>
  );
}
