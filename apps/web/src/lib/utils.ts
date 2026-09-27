import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';
import type { TaskEvent, TaskStepProgress } from './types';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Derive live step progress from ONLY the task's persisted real transition
 * events (step_started / step_completed / step_failed / step_skipped,
 * recovery_started, verification phase markers). Never fabricated: if no
 * transition events exist, there is no progress to show. The current step
 * is the most recent step_started with no later completion/failed/skipped
 * for the same step id (a re-start re-arms the step after recovery).
 */
export function deriveStepProgress(events: TaskEvent[] | undefined | null): TaskStepProgress | null {
  const evs = Array.isArray(events) ? events : [];
  const stepEvs = evs.filter((e) =>
    ['step_started', 'step_completed', 'step_failed', 'step_skipped'].includes(e.event_type)
  );
  if (stepEvs.length === 0 && !evs.some((e) => e.event_type === 'recovery_started')) {
    return null;
  }

  // Latest transition per step id, in event order.
  const last: Record<string, string> = {};
  for (const e of stepEvs) {
    const sid = (e.payload as Record<string, unknown> | null)?.step_id;
    if (typeof sid === 'string') last[sid] = e.event_type;
  }
  const states = Object.values(last);
  const doneIds = Object.entries(last)
    .filter(([, t]) => ['step_completed', 'step_failed', 'step_skipped'].includes(t))
    .map(([sid]) => sid);
  const currentEntry = Object.entries(last).find(([, t]) => t === 'step_started');

  const startsCount = {} as Record<string, number>;
  for (const e of stepEvs) {
    if (e.event_type === 'step_started') {
      const sid = (e.payload as Record<string, unknown> | null)?.step_id;
      if (typeof sid === 'string') startsCount[sid] = (startsCount[sid] || 0) + 1;
    }
  }

  const verifications = evs.filter(
    (e) => e.event_type === 'verification' && (e.payload as Record<string, unknown> | null)?.phase === 'started'
  );
  const lastStartedAttempt = verifications.length
    ? (verifications[verifications.length - 1].payload as Record<string, unknown> | null)?.attempt
    : null;
  const verifyingNow =
    lastStartedAttempt !== null &&
    !evs.some(
      (e) =>
        e.event_type === 'verification' &&
        (e.payload as Record<string, unknown> | null)?.phase !== 'started' &&
        (e.payload as Record<string, unknown> | null)?.attempt === lastStartedAttempt
    );

  const titles: Record<string, string> = {};
  for (const e of stepEvs) {
    const p = e.payload as Record<string, unknown> | null;
    if (p && typeof p.step_id === 'string' && typeof p.title === 'string') {
      titles[p.step_id] = p.title;
    }
  }

  return {
    current: currentEntry ? (titles[currentEntry[0]] || currentEntry[0]) : null,
    completed: states.filter((t) => t === 'step_completed').length,
    failed: states.filter((t) => t === 'step_failed').length,
    skipped: states.filter((t) => t === 'step_skipped').length,
    recoveries: evs.filter((e) => e.event_type === 'recovery_started').length,
    verifying: verifyingNow,
  };
}

/**
 * The backend serializes naive UTC datetimes (no timezone suffix). Parse them
 * as UTC so relative times aren't skewed by the viewer's local offset.
 */
function parseBackendDate(date: string | Date): Date {
  if (date instanceof Date) return date;
  return /[Zz]|[+-]\d{2}:?\d{2}$/.test(date) ? new Date(date) : new Date(`${date}Z`);
}

export function formatDate(date: string | Date): string {
  const d = parseBackendDate(date);
  return d.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatRelativeTime(date: string | Date): string {
  const d = parseBackendDate(date);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return formatDate(date);
}

export function getStatusColor(status: string): string {
  switch (status) {
    case 'completed':
      return 'bg-green-500';
    case 'failed':
      return 'bg-red-500';
    case 'cancelled':
      return 'bg-gray-500';
    case 'executing':
    case 'running':
      return 'bg-blue-500 animate-pulse';
    case 'waiting_for_user':
      return 'bg-yellow-500';
    case 'recovering':
      return 'bg-orange-500 animate-pulse';
    case 'understanding':
    case 'planning':
    case 'preparing':
      return 'bg-purple-500';
    case 'verifying':
      return 'bg-indigo-500';
    default:
      return 'bg-gray-500';
  }
}

export function getStatusLabel(status: string): string {
  switch (status) {
    case 'created':
      return 'Created';
    case 'understanding':
      return 'Understanding';
    case 'planning':
      return 'Planning';
    case 'preparing':
      return 'Preparing';
    case 'waiting_for_user':
      return 'Awaiting Input';
    case 'executing':
      return 'Executing';
    case 'verifying':
      return 'Verifying';
    case 'recovering':
      return 'Recovering';
    case 'completed':
      return 'Completed';
    case 'partially_completed':
      return 'Partial';
    case 'failed':
      return 'Failed';
    case 'cancelled':
      return 'Cancelled';
    default:
      return status;
  }
}

export function truncateText(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return text.slice(0, maxLength - 3) + '...';
}
