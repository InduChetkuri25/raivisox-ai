'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useAuth } from '@/components/providers';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { projectsApi } from '@/lib/api';
import { formatDate } from '@/lib/utils';
import type { Project } from '@/lib/types';

export default function ProjectsPage() {
  const { token } = useAuth();
  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [isCreating, setIsCreating] = useState(false);

  const load = useCallback(async () => {
    if (!token) return;
    setIsLoading(true);
    try {
      const data = await projectsApi.list(token);
      setProjects(data.projects ?? []);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load projects');
    } finally {
      setIsLoading(false);
    }
  }, [token]);

  useEffect(() => {
    load();
  }, [load]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !token) return;
    setIsCreating(true);
    setError('');
    try {
      await projectsApi.create(token, name.trim(), description.trim() || undefined);
      setName('');
      setDescription('');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create project');
    } finally {
      setIsCreating(false);
    }
  };

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
        <div>
          <h1 className="text-2xl font-bold mb-1">Projects</h1>
          <p className="text-muted-foreground text-sm">
            Group your tasks into projects. Tasks can be assigned to a project when created via the API.
          </p>
        </div>

        {error && (
          <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/40 text-sm text-red-400">
            {error}
          </div>
        )}

        <form onSubmit={handleCreate} className="p-5 rounded-xl bg-dark-800 border border-dark-700 space-y-3">
          <h2 className="text-sm font-semibold text-muted-foreground">Create project</h2>
          <div className="flex flex-col sm:flex-row gap-3">
            <Input
              type="text"
              placeholder="Project name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="flex-1 bg-dark-900"
              required
              maxLength={255}
            />
            <Button type="submit" disabled={!name.trim() || isCreating} className="bg-raivisox-600 hover:bg-raivisox-700">
              {isCreating ? 'Creating…' : 'Create Project'}
            </Button>
          </div>
          <Input
            type="text"
            placeholder="Description (optional)"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="bg-dark-900"
          />
        </form>

        {isLoading ? (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-20 bg-dark-800 rounded-xl animate-pulse" />
            ))}
          </div>
        ) : projects.length === 0 ? (
          <div className="text-center py-12 bg-dark-800 rounded-xl border border-dashed border-dark-600">
            <div className="text-4xl mb-4">📁</div>
            <p className="text-muted-foreground mb-1">No projects yet</p>
            <p className="text-sm text-muted-foreground">Create your first project above</p>
          </div>
        ) : (
          <div className="space-y-3">
            {projects.map((p) => (
              <Link
                key={p.id}
                href={`/projects/${p.id}`}
                className="block p-5 rounded-xl bg-dark-800 border border-dark-700 hover:border-raivisox-500/50 transition-colors"
              >
                <div className="flex items-center justify-between gap-4">
                  <div className="min-w-0">
                    <h3 className="font-semibold truncate">{p.name}</h3>
                    {p.description && (
                      <p className="text-sm text-muted-foreground truncate">{p.description}</p>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground shrink-0 text-right">
                    <div>{formatDate(p.created_at)}</div>
                    <div>{p.status}</div>
                  </div>
                </div>
              </Link>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
