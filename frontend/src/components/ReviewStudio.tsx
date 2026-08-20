'use client';

import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Play, Pause, Download, Edit2, Check, AudioLines
} from 'lucide-react';
import { SessionDetail, Turn, Speaker } from '@/types';
import { fetchSessionDetail, renameSpeaker, editTurn, getSessionAudioUrl } from '@/lib/api';

interface ReviewStudioProps {
  sessionId: string;
  onOpenExport: () => void;
}

export const ReviewStudio: React.FC<ReviewStudioProps> = ({ sessionId, onOpenExport }) => {
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  
  // Real HTML5 Audio playback state
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTimeMs, setCurrentTimeMs] = useState(0);
  const [playbackRate, setPlaybackRate] = useState(1);

  // Inline editing state
  const [editingTurnId, setEditingTurnId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState('');
  
  // Speaker renaming modal state
  const [renameSpeakerObj, setRenameSpeakerObj] = useState<Speaker | null>(null);
  const [newSpeakerName, setNewSpeakerName] = useState('');
  const [newSpeakerColor, setNewSpeakerColor] = useState('#4f46e5');

  const loadSession = useCallback(async () => {
    try {
      const data = await fetchSessionDetail(sessionId);
      setSession(data);
    } catch (err) {
      console.error(err);
    } finally {
      setIsLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    let isMounted = true;
    fetchSessionDetail(sessionId)
      .then((data) => {
        if (isMounted) {
          setSession(data);
          setIsLoading(false);
        }
      })
      .catch((err) => {
        console.error(err);
        if (isMounted) setIsLoading(false);
      });

    return () => {
      isMounted = false;
    };
  }, [sessionId]);

  const togglePlay = () => {
    if (!audioRef.current) return;
    if (isPlaying) {
      audioRef.current.pause();
    } else {
      audioRef.current.play().catch(() => {});
    }
    setIsPlaying(!isPlaying);
  };

  const seekToMs = (ms: number) => {
    if (audioRef.current) {
      audioRef.current.currentTime = ms / 1000.0;
      audioRef.current.play().catch(() => {});
      setIsPlaying(true);
    }
    setCurrentTimeMs(ms);
  };

  const handleAudioTimeUpdate = () => {
    if (audioRef.current) {
      setCurrentTimeMs(Math.floor(audioRef.current.currentTime * 1000));
    }
  };

  const handlePlaybackRateChange = (rate: number) => {
    setPlaybackRate(rate);
    if (audioRef.current) {
      audioRef.current.playbackRate = rate;
    }
  };

  const handleStartEditTurn = (turn: Turn) => {
    setEditingTurnId(turn.id);
    setEditingText(turn.text);
  };

  const handleSaveTurn = async (turnId: string) => {
    try {
      await editTurn(turnId, { text: editingText });
      setEditingTurnId(null);
      await loadSession();
    } catch (err) {
      console.error(err);
    }
  };

  const handleSpeakerReassign = async (turnId: string, speakerId: string) => {
    try {
      await editTurn(turnId, { speaker_id: speakerId });
      await loadSession();
    } catch (err) {
      console.error(err);
    }
  };

  const handleOpenRenameSpeaker = (speaker: Speaker) => {
    setRenameSpeakerObj(speaker);
    setNewSpeakerName(speaker.display_name);
    setNewSpeakerColor(speaker.color);
  };

  const handleSaveSpeakerRename = async () => {
    if (!renameSpeakerObj || !newSpeakerName.trim()) return;
    try {
      await renameSpeaker(renameSpeakerObj.id, {
        display_name: newSpeakerName.trim(),
        color: newSpeakerColor,
      });
      setRenameSpeakerObj(null);
      await loadSession();
    } catch (err) {
      console.error(err);
    }
  };

  const formatMs = (ms: number) => {
    const totalSecs = Math.floor(ms / 1000);
    const m = Math.floor(totalSecs / 60);
    const s = totalSecs % 60;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  if (isLoading || !session) {
    return (
      <div style={{ textAlign: 'center', padding: '100px 20px', color: 'var(--text-muted)' }}>
        <p>Loading review studio...</p>
      </div>
    );
  }

  const durationMs = session.duration_ms || (session.turns.length ? session.turns[session.turns.length - 1].end_ms : 60000);
  const audioSourceUrl = getSessionAudioUrl(session.id);
  const activityById = new Map(
    session.speaker_activities.map((activity) => [activity.id, activity])
  );

  return (
    <div style={{ maxWidth: 1080, margin: '0 auto' }}>
      {/* Hidden native HTML5 audio element */}
      <audio
        ref={audioRef}
        src={audioSourceUrl}
        onTimeUpdate={handleAudioTimeUpdate}
        onEnded={() => setIsPlaying(false)}
      />

      {/* Review Studio Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24, flexWrap: 'wrap', gap: 16 }}>
        <div>
          <h1 style={{ fontSize: '1.8rem', color: 'white', marginBottom: 4 }}>{session.title}</h1>
          <div style={{ display: 'flex', gap: 12, fontSize: '0.85rem', color: 'var(--text-muted)' }}>
            <span>Language: <strong style={{ color: '#818cf8', textTransform: 'capitalize' }}>{session.language_mode}</strong></span>
            <span>•</span>
            <span>Duration: <strong style={{ color: 'white', fontFamily: 'var(--font-mono)' }}>{formatMs(durationMs)}</strong></span>
            <span>•</span>
            <span>Model: <strong>{session.asr_model}</strong></span>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 10 }}>
          <button className="btn-primary" onClick={onOpenExport}>
            <Download size={16} />
            <span>Export Transcript</span>
          </button>
        </div>
      </div>

      {/* Real HTML5 Audio Player Bar */}
      <div className="audio-player-bar">
        <button
          onClick={togglePlay}
          style={{
            width: 44,
            height: 44,
            borderRadius: '50%',
            background: 'var(--primary)',
            border: 'none',
            color: 'white',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            cursor: 'pointer',
            boxShadow: 'var(--shadow-glow)',
          }}
        >
          {isPlaying ? <Pause size={18} fill="currentColor" /> : <Play size={18} fill="currentColor" style={{ marginLeft: 2 }} />}
        </button>

        <span style={{ fontSize: '0.85rem', fontFamily: 'var(--font-mono)', color: 'var(--text-main)', minWidth: 45 }}>
          {formatMs(currentTimeMs)}
        </span>

        <input
          type="range"
          className="timeline-slider"
          min={0}
          max={durationMs}
          value={currentTimeMs}
          onChange={(e) => seekToMs(Number(e.target.value))}
        />

        <span style={{ fontSize: '0.85rem', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', minWidth: 45 }}>
          {formatMs(durationMs)}
        </span>

        <select
          className="select-field"
          style={{ padding: '6px 10px', fontSize: '0.8rem' }}
          value={playbackRate}
          onChange={(e) => handlePlaybackRateChange(Number(e.target.value))}
        >
          <option value={0.75}>0.75x</option>
          <option value={1}>1.0x</option>
          <option value={1.25}>1.25x</option>
          <option value={1.5}>1.5x</option>
        </select>
      </div>

      {session.overlap_regions.length > 0 && (
        <section className="overlap-panel" aria-labelledby="overlap-heading">
          <div className="overlap-panel-heading">
            <div className="overlap-panel-title">
              <AudioLines size={18} aria-hidden="true" />
              <div>
                <h2 id="overlap-heading">Overlap review</h2>
                <p>These regions contain simultaneous speech. Mixed words remain unattributed.</p>
              </div>
            </div>
            <span className="overlap-count">
              {session.overlap_regions.length} {session.overlap_regions.length === 1 ? 'region' : 'regions'}
            </span>
          </div>

          <div className="overlap-list">
            {session.overlap_regions.map((region) => {
              const activities = region.speaker_activity_ids
                .map((activityId) => activityById.get(activityId))
                .filter((activity) => activity !== undefined);
              const speakers = Array.from(
                new Map(activities.map((activity) => [activity.speaker_id, activity])).values()
              );
              const speakerNames = speakers
                .map((speaker) => speaker.speaker_name)
                .join(' and ') || 'unresolved speakers';
              const isActive = currentTimeMs >= region.start_ms
                && currentTimeMs <= region.end_ms;

              return (
                <button
                  type="button"
                  key={region.id}
                  className={`overlap-row${isActive ? ' active' : ''}`}
                  onClick={() => seekToMs(region.start_ms)}
                  aria-label={`Play overlap at ${formatMs(region.start_ms)} with ${speakerNames}`}
                >
                  <span className="overlap-time">
                    {formatMs(region.start_ms)}–{formatMs(region.end_ms)}
                  </span>
                  <span className="overlap-speakers">
                    {speakers.map((speaker) => (
                      <span
                        className="overlap-speaker"
                        key={speaker.speaker_id}
                        style={{ color: speaker.speaker_color }}
                      >
                        <span
                          className="overlap-speaker-dot"
                          style={{ backgroundColor: speaker.speaker_color }}
                        />
                        {speaker.speaker_name}
                      </span>
                    ))}
                  </span>
                  <span className="overlap-status">
                    {region.resolution_status === 'mixed_only'
                      ? 'Mixed audio'
                      : region.resolution_status.replaceAll('_', ' ')}
                  </span>
                </button>
              );
            })}
          </div>
        </section>
      )}

      {/* Speaker List Legend */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 20, flexWrap: 'wrap' }}>
        <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)', fontWeight: 500 }}>Speakers:</span>
        {session.speakers.map((spk) => (
          <div
            key={spk.id}
            onClick={() => handleOpenRenameSpeaker(spk)}
            className="speaker-tag"
            style={{
              backgroundColor: `${spk.color}20`,
              color: spk.color,
              border: `1px solid ${spk.color}50`,
              padding: '5px 12px',
              fontSize: '0.85rem',
            }}
            title="Click to rename speaker"
          >
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: spk.color }} />
            <span>{spk.display_name}</span>
            <Edit2 size={12} style={{ opacity: 0.7, marginLeft: 4 }} />
          </div>
        ))}
      </div>

      {/* Transcript Turns Studio */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        {session.turns.map((turn) => {
          const isActive = currentTimeMs >= turn.start_ms && currentTimeMs <= turn.end_ms;
          const isEditing = editingTurnId === turn.id;

          return (
            <div
              key={turn.id}
              className="glass-card"
              style={{
                padding: '20px 24px',
                borderColor: isActive ? 'var(--primary)' : undefined,
                background: isActive ? 'rgba(99, 102, 241, 0.08)' : undefined,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <div
                    className="speaker-tag"
                    style={{
                      backgroundColor: `${turn.speaker_color || '#4f46e5'}20`,
                      color: turn.speaker_color || '#818cf8',
                      border: `1px solid ${turn.speaker_color || '#4f46e5'}40`,
                    }}
                  >
                    <span style={{ width: 6, height: 6, borderRadius: '50%', background: turn.speaker_color || '#4f46e5' }} />
                    <span>{turn.speaker_name || 'Speaker 1'}</span>
                  </div>

                  <select
                    className="select-field"
                    style={{ padding: '2px 8px', fontSize: '0.75rem', background: 'transparent' }}
                    value={turn.speaker_id || ''}
                    onChange={(e) => handleSpeakerReassign(turn.id, e.target.value)}
                  >
                    {session.speakers.map((s) => (
                      <option key={s.id} value={s.id} style={{ background: '#0e131f' }}>
                        {s.display_name}
                      </option>
                    ))}
                  </select>

                  <button
                    onClick={() => seekToMs(turn.start_ms)}
                    className="turn-time"
                    style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px 6px', borderRadius: 4 }}
                    title="Play from here"
                  >
                    ▶ {formatMs(turn.start_ms)}
                  </button>
                </div>

                <div>
                  {isEditing ? (
                    <button
                      className="btn-primary"
                      onClick={() => handleSaveTurn(turn.id)}
                      style={{ padding: '4px 12px', fontSize: '0.8rem' }}
                    >
                      <Check size={14} />
                      <span>Save</span>
                    </button>
                  ) : (
                    <button
                      className="btn-secondary"
                      onClick={() => handleStartEditTurn(turn)}
                      style={{ padding: '4px 10px', fontSize: '0.8rem' }}
                    >
                      <Edit2 size={13} />
                      <span>Edit</span>
                    </button>
                  )}
                </div>
              </div>

              {isEditing ? (
                <textarea
                  className="input-field"
                  rows={3}
                  value={editingText}
                  onChange={(e) => setEditingText(e.target.value)}
                  style={{ lineHeight: 1.6, fontSize: '1rem' }}
                />
              ) : (
                <div
                  className="turn-text"
                  onClick={() => handleStartEditTurn(turn)}
                  style={{ cursor: 'text' }}
                >
                  {turn.text}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Rename Speaker Modal */}
      {renameSpeakerObj && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100, backdropFilter: 'blur(8px)' }}>
          <div className="glass-card" style={{ maxWidth: 420, width: '100%', padding: 28, background: '#0e131f' }}>
            <h3 style={{ fontSize: '1.2rem', marginBottom: 16 }}>Rename Speaker</h3>
            
            <div style={{ marginBottom: 16 }}>
              <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)', display: 'block', marginBottom: 6 }}>
                Speaker Display Name
              </label>
              <input
                type="text"
                className="input-field"
                value={newSpeakerName}
                onChange={(e) => setNewSpeakerName(e.target.value)}
                autoFocus
              />
            </div>

            <div style={{ marginBottom: 24 }}>
              <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)', display: 'block', marginBottom: 6 }}>
                Speaker Color Tag
              </label>
              <div style={{ display: 'flex', gap: 10 }}>
                {['#4f46e5', '#8b5cf6', '#06b6d4', '#10b981', '#f59e0b', '#f43f5e', '#ec4899'].map((c) => (
                  <button
                    key={c}
                    onClick={() => setNewSpeakerColor(c)}
                    style={{
                      width: 32,
                      height: 32,
                      borderRadius: '50%',
                      background: c,
                      border: newSpeakerColor === c ? '2px solid white' : 'none',
                      cursor: 'pointer',
                    }}
                  />
                ))}
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
              <button className="btn-secondary" onClick={() => setRenameSpeakerObj(null)}>
                Cancel
              </button>
              <button className="btn-primary" onClick={handleSaveSpeakerRename}>
                Save Changes
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
