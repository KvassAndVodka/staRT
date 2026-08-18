export interface Word {
  id: string;
  start_ms: number;
  end_ms: number;
  text: string;
  speaker_id?: string | null;
  stability: 'provisional' | 'committed' | 'finalized';
  confidence?: number | null;
  language?: string | null;
}

export interface Speaker {
  id: string;
  machine_label: string;
  display_name: string;
  color: string;
  sort_order: number;
}

export interface Turn {
  id: string;
  speaker_id?: string | null;
  speaker_name?: string;
  speaker_color?: string;
  start_ms: number;
  end_ms: number;
  text: string;
  edited_text?: string | null;
  break_reason?: string;
  words?: Word[];
}

export interface AudioAsset {
  id: string;
  session_id: string;
  kind: string;
  status: string;
  container?: string | null;
  codec?: string | null;
  sample_rate_hz?: number | null;
  channels?: number | null;
  duration_ms?: number | null;
  size_bytes?: number | null;
  sha256?: string | null;
}

export interface SessionSummary {
  id: string;
  title: string;
  source_url: string;
  source_type: string;
  status: 'queued' | 'connecting' | 'live' | 'finalizing' | 'ready' | 'failed' | 'cancelled';
  processing_mode: 'normal' | 'catching_up' | 'degraded' | 'record_only' | 'recovering_source';
  language_mode: string;
  duration_ms?: number | null;
  created_at: string;
  updated_at: string;
  asr_model: string;
  diarization_model: string;
  speaker_count: number;
  deleted_at?: string | null;
}

export interface SessionDetail extends SessionSummary {
  speakers: Speaker[];
  turns: Turn[];
  audio_assets: AudioAsset[];
  audio_assets_count: number;
  last_durable_audio_ms: number;
  committed_frontier_ms: number;
  training_consent: string;
}

export interface StorageSummary {
  total_sessions: number;
  active_sessions: number;
  trashed_sessions: number;
  total_audio_bytes: number;
  total_export_bytes: number;
}

export interface HealthInfo {
  status: string;
  app: string;
  version: string;
  cuda_devices: number;
  default_device: string;
  default_model: string;
  default_compute_type: string;
}

export interface WebSocketEvent {
  session_id: string;
  type: string;
  sequence: number;
  payload: Record<string, unknown>;
  version: string;
}
