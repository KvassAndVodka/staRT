import { expect, test, type Page } from '@playwright/test';
import type {
  HealthInfo,
  SessionDetail,
  SessionSummary,
  StorageSummary,
  Turn,
} from '../../src/types';

const readySession: SessionSummary = {
  id: 'session-ready',
  title: 'Architecture Lecture',
  source_url: 'https://example.com/lecture.mp3',
  source_type: 'finite',
  status: 'ready',
  processing_mode: 'normal',
  language_mode: 'auto-mixed',
  duration_ms: 92_000,
  created_at: '2026-08-20T01:00:00Z',
  updated_at: '2026-08-20T01:02:00Z',
  asr_model: 'small',
  diarization_model: 'community-1',
  speaker_count: 2,
};

const liveSession: SessionSummary = {
  ...readySession,
  id: 'session-live',
  title: 'Live Architecture Session',
  source_url: 'https://example.com/live.m3u8',
  source_type: 'live',
  status: 'live',
  duration_ms: 12_000,
  speaker_count: 1,
};

const liveTurn: Turn = {
  id: 'turn-live',
  speaker_id: 'speaker-1',
  speaker_name: 'Speaker 1',
  speaker_color: '#4f46e5',
  start_ms: 12_000,
  end_ms: 14_000,
  text: 'The live transcript is connected.',
};

const readyDetail: SessionDetail = {
  ...readySession,
  speakers: [
    {
      id: 'speaker-1',
      machine_label: 'SPEAKER_00',
      display_name: 'Javier',
      color: '#4f46e5',
      sort_order: 0,
    },
  ],
  turns: [
    {
      id: 'turn-ready',
      speaker_id: 'speaker-1',
      speaker_name: 'Javier',
      speaker_color: '#4f46e5',
      start_ms: 0,
      end_ms: 4_000,
      text: 'The repository now has browser coverage.',
    },
  ],
  audio_assets: [],
  speaker_activities: [],
  overlap_regions: [],
  audio_assets_count: 0,
  last_durable_audio_ms: 92_000,
  committed_frontier_ms: 92_000,
  event_sequence: 4,
  event_replay_floor: 0,
  training_consent: 'not_requested',
};

const liveDetail: SessionDetail = {
  ...readyDetail,
  ...liveSession,
  speakers: [],
  turns: [],
  last_durable_audio_ms: 12_000,
  committed_frontier_ms: 10_000,
  event_sequence: 0,
};

const health: HealthInfo = {
  status: 'ok',
  app: 'staRT',
  version: '0.1.0',
  cuda_devices: 1,
  default_device: 'cuda',
  default_model: 'small',
  default_compute_type: 'int8_float16',
};

const storage: StorageSummary = {
  total_sessions: 2,
  active_sessions: 1,
  trashed_sessions: 1,
  total_audio_bytes: 1_048_576,
  total_export_bytes: 2_048,
};

interface ApiOptions {
  sessions?: SessionSummary[];
  healthAvailable?: boolean;
  rejectCreate?: boolean;
}

async function mockApi(page: Page, options: ApiOptions = {}) {
  let created = false;
  let createPayload: Record<string, unknown> | null = null;
  const sessions = options.sessions ?? [readySession];

  await page.route('http://127.0.0.1:8000/api/**', async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());

    if (pathname === '/api/health') {
      await route.fulfill(
        options.healthAvailable === false
          ? { status: 503, json: { detail: 'Backend unavailable' } }
          : { json: health },
      );
      return;
    }

    if (pathname === '/api/sessions' && request.method() === 'GET') {
      await route.fulfill({ json: created ? [liveSession, ...sessions] : sessions });
      return;
    }

    if (pathname === '/api/sessions' && request.method() === 'POST') {
      createPayload = request.postDataJSON() as Record<string, unknown>;
      if (options.rejectCreate) {
        await route.fulfill({
          status: 422,
          json: { detail: 'The source URL is not allowed.' },
        });
        return;
      }
      created = true;
      await route.fulfill({ status: 201, json: liveSession });
      return;
    }

    if (pathname === '/api/sessions/trash') {
      await route.fulfill({ json: [] });
      return;
    }

    if (pathname === '/api/storage') {
      await route.fulfill({ json: storage });
      return;
    }

    if (pathname === `/api/sessions/${liveSession.id}`) {
      await route.fulfill({ json: liveDetail });
      return;
    }

    if (pathname === `/api/sessions/${readySession.id}`) {
      await route.fulfill({ json: readyDetail });
      return;
    }

    if (pathname.endsWith('/audio')) {
      await route.fulfill({ status: 204 });
      return;
    }

    await route.fulfill({
      status: 501,
      json: { detail: `No smoke-test mock for ${request.method()} ${pathname}` },
    });
  });

  return {
    createPayload: () => createPayload,
  };
}

test('shows backend health and recent transcripts', async ({ page }) => {
  await mockApi(page);

  await page.goto('/');

  await expect(page.getByRole('heading', { name: /Transcribe Any Stream/ })).toBeVisible();
  await expect(page.getByText('CUDA GPU: small (int8_float16)')).toBeVisible();
  await expect(page.getByText('Recent Transcripts')).toBeVisible();
  await expect(page.getByText(readySession.title)).toBeVisible();
});

test('starts a configured session and receives a live turn', async ({ page }) => {
  const api = await mockApi(page, { sessions: [] });
  await page.routeWebSocket('ws://127.0.0.1:8000/api/**', (socket) => {
    setTimeout(() => {
      socket.send(JSON.stringify({
        session_id: liveSession.id,
        type: 'turn.upsert',
        sequence: 1,
        payload: { turns: [liveTurn] },
        version: '1',
      }));
    }, 50);
  });

  await page.goto('/');
  await page.getByPlaceholder(/Paste an audio, video, or live-stream link/).fill(liveSession.source_url);
  await page.getByRole('combobox').nth(0).selectOption('en');
  await page.getByRole('combobox').nth(1).selectOption('turbo');
  await page.getByRole('button', { name: 'Transcribe' }).click();

  await expect(page.getByRole('heading', { name: liveSession.title })).toBeVisible();
  await expect(page.getByText('LIVE', { exact: true })).toBeVisible();
  await expect(page.getByText(liveTurn.text)).toBeVisible();
  expect(api.createPayload()).toEqual({
    url: liveSession.source_url,
    language_mode: 'en',
    asr_model: 'turbo',
  });
});

test('opens a transcript from history and shows export options', async ({ page }) => {
  await mockApi(page);

  await page.goto('/');
  await page.getByRole('button', { name: 'History & Trash' }).click();

  await expect(page.getByRole('button', { name: 'Active Sessions (1)' })).toBeVisible();
  await expect(page.getByText(readySession.title)).toBeVisible();
  await page.getByRole('button', { name: 'Review', exact: true }).click();

  await expect(page.getByRole('heading', { name: readySession.title })).toBeVisible();
  await expect(page.getByText('The repository now has browser coverage.')).toBeVisible();
  await page.getByRole('button', { name: 'Export Transcript' }).click();

  await expect(page.getByRole('heading', { name: 'Export: Export Transcript' })).toBeVisible();
  await expect(page.getByText('Plain Text (.txt)')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Download File' })).toHaveAttribute(
    'href',
    new RegExp(`/sessions/${readySession.id}/export\\?`),
  );
});

test('shows a server error when session creation is rejected', async ({ page }) => {
  await mockApi(page, {
    sessions: [],
    healthAvailable: false,
    rejectCreate: true,
  });

  await page.goto('/');
  await expect(page.getByText('Connecting...')).toBeVisible();
  await page.getByPlaceholder(/Paste an audio, video, or live-stream link/).fill('https://blocked.example/audio');
  await page.getByRole('button', { name: 'Transcribe' }).click();

  await expect(page.getByText('The source URL is not allowed.')).toBeVisible();
});
