'use client';

import { Fragment, useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useAuth } from '@/components/providers';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { auditApi } from '@/lib/api';
import { formatDate } from '@/lib/utils';
import type { AuditEvent } from '@/lib/types';

export default function AuditPage() {
  const { token } = useAuth();
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const [forbidden, setForbidden] = useState(false);
  const [actionFilter, setActionFilter] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!token) return;
    setIsLoading(true);
    try {
      const data = await auditApi.list(token, actionFilter || undefined);
      setEvents(data.events ?? []);
      setForbidden(false);
      setError('');
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to load audit trail';
      if (msg.includes('403') || msg.toLowerCase().includes('admin')) {
        setForbidden(true);
      }
      setError(msg);
    } finally {
      setIsLoading(false);
    }
  }, [token, actionFilter]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="min-h-screen bg-dark-900">
      <header className="border-b border-dark-700 bg-dark-800/50 backdrop-blur sticky top-0 z-50">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
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

      <main className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold mb-1">Audit Trail</h1>
            <p className="text-muted-foreground text-sm">
              Durable, DB-backed security events. Admin permission required.
            </p>
          </div>
          <div className="flex gap-2">
            <select
              value={actionFilter}
              onChange={(e) => setActionFilter(e.target.value)}
              className="bg-dark-800 border border-dark-600 rounded-lg text-sm px-3 py-2"
              aria-label="Filter by action"
            >
              <option value="">All actions</option>
              <option value="tool:codegen">tool:codegen</option>
              <option value="tool:fs_write">tool:fs_write</option>
              <option value="tool:fs_read">tool:fs_read</option>
              <option value="tool:shell_exec">tool:shell_exec</option>
              <option value="tool:python_exec">tool:python_exec</option>
              <option value="tool:http_get">tool:http_get</option>
              <option value="tool:finalize">tool:finalize</option>
              <option value="api:view_audit">api:view_audit</option>
            </select>
            <Button variant="outline" size="sm" onClick={load} disabled={isLoading}>
              Refresh
            </Button>
          </div>
        </div>

        {forbidden && (
          <div className="p-6 rounded-xl bg-red-500/10 border border-red-500/40 text-center">
            <div className="text-4xl mb-3">🔒</div>
            <h2 className="font-bold mb-1">Admin permission required</h2>
            <p className="text-sm text-muted-foreground">
              The audit trail is security evidence. Your account does not carry the admin
              permission, and the frontend cannot (and will not) bypass the backend check.
            </p>
          </div>
        )}

        {error && !forbidden && (
          <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/40 text-sm text-red-400">
            {error}
          </div>
        )}

        {isLoading ? (
          <div className="space-y-2">
            {[1, 2, 3, 4, 5].map((i) => (
              <div key={i} className="h-12 bg-dark-800 rounded-lg animate-pulse" />
            ))}
          </div>
        ) : !forbidden && events.length === 0 ? (
          <div className="text-center py-12 bg-dark-800 rounded-xl border border-dashed border-dark-600">
            <div className="text-4xl mb-4">🛡️</div>
            <p className="text-muted-foreground mb-1">No audit events</p>
            <p className="text-sm text-muted-foreground">
              Events appear here once the pipeline runs. They are persisted across restarts.
            </p>
          </div>
        ) : !forbidden && events.length > 0 ? (
          <div className="rounded-xl bg-dark-800 border border-dark-700 overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-muted-foreground border-b border-dark-700">
                  <th className="px-4 py-3">Time</th>
                  <th className="px-4 py-3">Actor</th>
                  <th className="px-4 py-3">Action</th>
                  <th className="px-4 py-3">Resource</th>
                  <th className="px-4 py-3">Result</th>
                  <th className="px-4 py-3"></th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <Fragment key={e.event_id}>
                    <tr className="border-b border-dark-700/60 hover:bg-dark-900/40">
                      <td className="px-4 py-2.5 text-xs whitespace-nowrap">{formatDate(e.timestamp)}</td>
                      <td className="px-4 py-2.5 text-xs font-mono">{e.actor_id}</td>
                      <td className="px-4 py-2.5 font-mono text-xs text-raivisox-400">{e.action}</td>
                      <td className="px-4 py-2.5 font-mono text-xs break-all">{e.resource}</td>
                      <td className="px-4 py-2.5">
                        <Badge variant={e.allowed ? 'success' : 'destructive'}>
                          {e.allowed ? 'allowed' : 'DENIED'}
                        </Badge>
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        {e.details && Object.keys(e.details).length > 0 && (
                          <button
                            type="button"
                            className="text-xs text-raivisox-400 hover:text-raivisox-300"
                            onClick={() => setExpanded(expanded === e.event_id ? null : e.event_id)}
                          >
                            {expanded === e.event_id ? 'Hide ▲' : 'Details ▼'}
                          </button>
                        )}
                      </td>
                    </tr>
                    {expanded === e.event_id && (
                      <tr className="bg-dark-900/60">
                        <td colSpan={6} className="px-4 py-3">
                          <pre className="text-[10px] leading-relaxed text-muted-foreground overflow-auto max-h-40">
                            {JSON.stringify(e.details, null, 2)}
                          </pre>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </main>
    </div>
  );
}
