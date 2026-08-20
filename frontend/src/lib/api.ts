import { SessionSummary, SessionDetail, StorageSummary, HealthInfo } from '@/types';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000/api';

export async function fetchHealth(): Promise<HealthInfo> {
  const res = await fetch(`${API_BASE}/health`);
  if (!res.ok) throw new Error('Failed to fetch health status');
  return res.json();
}

export async function fetchSessions(): Promise<SessionSummary[]> {
  const res = await fetch(`${API_BASE}/sessions`);
  if (!res.ok) throw new Error('Failed to fetch sessions');
  return res.json();
}

export async function fetchTrashedSessions(): Promise<SessionSummary[]> {
  const res = await fetch(`${API_BASE}/sessions/trash`);
  if (!res.ok) throw new Error('Failed to fetch trash');
  return res.json();
}

export async function fetchSessionDetail(id: string): Promise<SessionDetail> {
  const res = await fetch(`${API_BASE}/sessions/${id}`);
  if (!res.ok) throw new Error('Failed to fetch session detail');
  return res.json();
}

export async function createSession(data: {
  url: string;
  language_mode: string;
  asr_model?: string;
}): Promise<SessionSummary> {
  const res = await fetch(`${API_BASE}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to create session' }));
    throw new Error(err.detail || 'Failed to create session');
  }
  return res.json();
}

export async function stopSession(id: string): Promise<void> {
  await fetch(`${API_BASE}/sessions/${id}/stop`, { method: 'POST' });
}

export async function trashSession(id: string): Promise<void> {
  await fetch(`${API_BASE}/sessions/${id}`, { method: 'DELETE' });
}

export async function restoreSession(id: string): Promise<void> {
  await fetch(`${API_BASE}/sessions/${id}/restore`, { method: 'POST' });
}

export async function purgeSession(id: string): Promise<void> {
  await fetch(`${API_BASE}/sessions/${id}/purge`, { method: 'DELETE' });
}

export async function renameSpeaker(
  speakerId: string,
  data: { display_name: string; color?: string }
): Promise<void> {
  const res = await fetch(`${API_BASE}/speakers/${speakerId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to rename speaker');
}

export async function editTurn(
  turnId: string,
  data: { text?: string; speaker_id?: string }
): Promise<void> {
  const res = await fetch(`${API_BASE}/turns/${turnId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to edit turn');
}

export async function fetchStorageSummary(): Promise<StorageSummary> {
  const res = await fetch(`${API_BASE}/storage`);
  if (!res.ok) throw new Error('Failed to fetch storage summary');
  return res.json();
}

export function getSessionAudioUrl(sessionId: string): string {
  return `${API_BASE}/sessions/${sessionId}/audio`;
}

export function getExportUrl(
  sessionId: string,
  format: string,
  includeTimestamps = true,
  includeSpeakers = true,
  revision = 'edited'
): string {
  return `${API_BASE}/sessions/${sessionId}/export?format=${format}&revision=${revision}&include_timestamps=${includeTimestamps}&include_speakers=${includeSpeakers}`;
}

export function getWebSocketUrl(sessionId: string, sinceSequence: number): string {
  const wsBase = API_BASE.replace(/^http/, 'ws');
  const cursor = encodeURIComponent(String(sinceSequence));
  return `${wsBase}/sessions/${sessionId}/events?since_sequence=${cursor}`;
}
