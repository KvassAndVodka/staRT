'use client';

import React, { useState } from 'react';
import { Play, Link2, Sparkles, Clock, Globe, Cpu, AlertCircle } from 'lucide-react';
import { SessionSummary } from '@/types';
import { createSession } from '@/lib/api';

interface HomeScreenProps {
  recentSessions: SessionSummary[];
  onSessionStarted: (sessionId: string) => void;
  onOpenSession: (sessionId: string) => void;
}

export const HomeScreen: React.FC<HomeScreenProps> = ({
  recentSessions,
  onSessionStarted,
  onOpenSession,
}) => {
  const [url, setUrl] = useState('');
  const [languageMode, setLanguageMode] = useState('auto-mixed');
  const [asrModel, setAsrModel] = useState('small');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;

    setError(null);
    setIsLoading(true);

    try {
      const session = await createSession({
        url: url.trim(),
        language_mode: languageMode,
        asr_model: asrModel,
      });
      onSessionStarted(session.id);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to start transcription session';
      setError(msg);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 840, margin: '0 auto' }}>
      {/* Hero Header */}
      <div style={{ textAlign: 'center', marginBottom: 40 }}>
        <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '6px 14px', borderRadius: 9999, background: 'rgba(99, 102, 241, 0.1)', border: '1px solid rgba(99, 102, 241, 0.25)', color: '#a5b4fc', fontSize: '0.85rem', marginBottom: 18 }}>
          <Sparkles size={15} />
          <span>Local-First Speech AI with Speaker Diarization</span>
        </div>
        <h1 style={{ fontSize: '2.8rem', lineHeight: 1.15, marginBottom: 16 }}>
          Transcribe Any Stream <br />
          <span className="gradient-accent">In Real Time, Fully Private.</span>
        </h1>
        <p style={{ color: 'var(--text-muted)', fontSize: '1.05rem', maxWidth: 580, margin: '0 auto' }}>
          Zero per-minute cost. Accepts public audio/video URLs, separates recurring speakers, and preserves continuous speech across window boundaries.
        </p>
      </div>

      {/* Main Link Input Card */}
      <div className="glass-card" style={{ marginBottom: 32 }}>
        <form onSubmit={handleSubmit}>
          <div style={{ display: 'flex', gap: 12, marginBottom: 20 }}>
            <div style={{ position: 'relative', flex: 1 }}>
              <Link2 size={20} style={{ position: 'absolute', left: 16, top: '50%', transform: 'translateY(-50%)', color: '#64748b' }} />
              <input
                type="url"
                className="input-field"
                style={{ paddingLeft: 46 }}
                placeholder="Paste an audio, video, or live-stream link (e.g. YouTube, HLS, direct MP3)..."
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                required
              />
            </div>
            <button
              type="submit"
              className="btn-primary"
              disabled={isLoading || !url.trim()}
              style={{ minWidth: 160 }}
            >
              {isLoading ? (
                <span>Starting...</span>
              ) : (
                <>
                  <Play size={18} fill="currentColor" />
                  <span>Transcribe</span>
                </>
              )}
            </button>
          </div>

          {error && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px', borderRadius: 8, background: 'rgba(244, 63, 94, 0.12)', border: '1px solid rgba(244, 63, 94, 0.25)', color: '#fecdd3', fontSize: '0.875rem', marginBottom: 18 }}>
              <AlertCircle size={16} />
              <span>{error}</span>
            </div>
          )}

          {/* Configuration Grid */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, paddingTop: 16, borderTop: '1px solid var(--border-subtle)' }}>
            <div>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: 6, fontWeight: 500 }}>
                <Globe size={14} />
                <span>Language Recognition Mode</span>
              </label>
              <select
                className="select-field"
                style={{ width: '100%' }}
                value={languageMode}
                onChange={(e) => setLanguageMode(e.target.value)}
              >
                <option value="auto-mixed">Auto-Mixed (English, Tagalog, Cebuano)</option>
                <option value="en">English Only (en)</option>
                <option value="tl">Tagalog / Filipino Only (tl)</option>
                <option value="auto">Multilingual Auto-Detect</option>
              </select>
            </div>

            <div>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: 6, fontWeight: 500 }}>
                <Cpu size={14} />
                <span>Model Profile & Hardware</span>
              </label>
              <select
                className="select-field"
                style={{ width: '100%' }}
                value={asrModel}
                onChange={(e) => setAsrModel(e.target.value)}
              >
                <option value="small">Whisper Small (Recommended for 4GB RTX 3050 Ti)</option>
                <option value="turbo">Whisper Turbo (Recommended for 6GB RTX 3050)</option>
                <option value="base">Whisper Base (Ultra Fast / Low Compute)</option>
                <option value="tiny">Whisper Tiny (Minimal VRAM / Fast)</option>
              </select>
            </div>
          </div>
        </form>
      </div>

      {/* Recent Sessions Tray */}
      {recentSessions.length > 0 && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
            <h3 style={{ fontSize: '1.1rem', color: 'var(--text-main)', display: 'flex', alignItems: 'center', gap: 8 }}>
              <Clock size={16} color="#818cf8" />
              <span>Recent Transcripts</span>
            </h3>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {recentSessions.slice(0, 4).map((s) => (
              <div
                key={s.id}
                className="glass-card"
                style={{ padding: '16px 20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', cursor: 'pointer' }}
                onClick={() => onOpenSession(s.id)}
              >
                <div>
                  <div style={{ fontWeight: 600, fontSize: '0.95rem', marginBottom: 4, color: 'var(--text-main)' }}>
                    {s.title}
                  </div>
                  <div style={{ display: 'flex', gap: 12, fontSize: '0.78rem', color: 'var(--text-faint)' }}>
                    <span>{new Date(s.created_at).toLocaleDateString()}</span>
                    <span>•</span>
                    <span>{s.speaker_count} Speaker{s.speaker_count === 1 ? '' : 's'}</span>
                    <span>•</span>
                    <span style={{ textTransform: 'capitalize' }}>{s.language_mode}</span>
                  </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span className={`badge ${s.status === 'live' ? 'badge-live' : ''}`} style={{ background: s.status === 'ready' ? 'rgba(16, 185, 129, 0.12)' : undefined, color: s.status === 'ready' ? '#6ee7b7' : undefined }}>
                    {s.status.toUpperCase()}
                  </span>
                  <button className="btn-secondary" style={{ padding: '6px 14px', fontSize: '0.8rem' }}>
                    Open
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};
