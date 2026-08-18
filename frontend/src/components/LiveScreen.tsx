'use client';

import React, { useState, useEffect, useRef } from 'react';
import { Square, ArrowDown, Radio } from 'lucide-react';
import { Turn, Word, WebSocketEvent } from '@/types';
import { getWebSocketUrl, stopSession, fetchSessionDetail } from '@/lib/api';

interface LiveScreenProps {
  sessionId: string;
  onFinalized: (sessionId: string) => void;
}

export const LiveScreen: React.FC<LiveScreenProps> = ({ sessionId, onFinalized }) => {
  const [sessionTitle, setSessionTitle] = useState('Connecting to live media...');
  const [status, setStatus] = useState<'connecting' | 'live' | 'finalizing' | 'ready'>('connecting');
  const [processingMode, setProcessingMode] = useState('normal');
  const [elapsedMs, setElapsedMs] = useState(0);
  
  const [turns, setTurns] = useState<Turn[]>([]);
  const [provisionalWords, setProvisionalWords] = useState<Word[]>([]);
  
  const [autoScroll, setAutoScroll] = useState(true);
  const [isStopping, setIsStopping] = useState(false);
  
  const streamEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const readyRef = useRef(false);
  const onFinalizedRef = useRef(onFinalized);
  const seenEventIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    onFinalizedRef.current = onFinalized;
  }, [onFinalized]);

  // Poll/Sync session metadata initially
  useEffect(() => {
    let isMounted = true;
    fetchSessionDetail(sessionId)
      .then((detail) => {
        if (isMounted) {
          if (detail.title) setSessionTitle(detail.title);
          if (detail.turns && detail.turns.length > 0) setTurns(detail.turns);
          if (detail.status === 'ready') {
            readyRef.current = true;
            onFinalizedRef.current(sessionId);
          }
        }
      })
      .catch((err) => console.error(err));

    return () => {
      isMounted = false;
    };
  }, [sessionId]);

  // Connect WebSocket with reconnect backoff
  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
    let isSubscribed = true;
    readyRef.current = false;
    seenEventIdsRef.current.clear();

    const syncSnapshot = async () => {
      try {
        const detail = await fetchSessionDetail(sessionId);
        if (!isSubscribed) return;
        if (detail.title) setSessionTitle(detail.title);
        setTurns(detail.turns || []);
        setProvisionalWords([]);
        if (detail.duration_ms) setElapsedMs(detail.duration_ms);
        if (detail.status === 'ready') {
          readyRef.current = true;
          onFinalizedRef.current(sessionId);
        } else if (['connecting', 'live', 'finalizing'].includes(detail.status)) {
          setStatus(detail.status as 'connecting' | 'live' | 'finalizing');
        }
        setProcessingMode(detail.processing_mode);
      } catch (err) {
        console.error('[Live WS] Snapshot sync failed:', err);
      }
    };

    const connect = () => {
      if (!isSubscribed) return;
      const wsUrl = getWebSocketUrl(sessionId);
      ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      ws.onopen = () => {
        // A snapshot closes any event gap caused by a disconnected socket.
        void syncSnapshot();
      };

      ws.onmessage = (event) => {
        try {
          const data: WebSocketEvent = JSON.parse(event.data);
          const { type, payload } = data;
          const eventId = typeof payload.event_id === 'string' ? payload.event_id : null;
          if (eventId) {
            if (seenEventIdsRef.current.has(eventId)) return;
            seenEventIdsRef.current.add(eventId);
            if (seenEventIdsRef.current.size > 2000) {
              const oldest = seenEventIdsRef.current.values().next().value;
              if (oldest) seenEventIdsRef.current.delete(oldest);
            }
          }

          if (type === 'session.status') {
            if (typeof payload.status === 'string') {
              setStatus(payload.status as 'connecting' | 'live' | 'finalizing' | 'ready');
            }
            if (typeof payload.title === 'string') setSessionTitle(payload.title);
            if (typeof payload.processing_mode === 'string') setProcessingMode(payload.processing_mode);
          } else if (type === 'source.metadata') {
            if (typeof payload.title === 'string') setSessionTitle(payload.title);
          } else if (type === 'source.reconnecting') {
            setProcessingMode('recovering_source');
          } else if (type === 'source.reconnected') {
            setProcessingMode('normal');
            if (typeof payload.gap_end_ms === 'number') {
              setElapsedMs(payload.gap_end_ms);
            }
          } else if (type === 'transcript.partial') {
            if (Array.isArray(payload.provisional_words)) {
              setProvisionalWords(payload.provisional_words as Word[]);
            }
            if (typeof payload.current_time_ms === 'number') {
              setElapsedMs(payload.current_time_ms);
            }
          } else if (type === 'turn.upsert') {
            if (Array.isArray(payload.turns)) {
              setTurns(payload.turns as Turn[]);
            }
          } else if (type === 'session.ready') {
            readyRef.current = true;
            setStatus('ready');
            onFinalizedRef.current(sessionId);
          }
        } catch (err) {
          console.error('[Live WS] Message parsing error:', err);
        }
      };

      ws.onclose = () => {
        if (isSubscribed && !readyRef.current) {
          reconnectTimeout = setTimeout(connect, 2000);
        }
      };
    };

    connect();

    return () => {
      isSubscribed = false;
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      if (ws) ws.close();
    };
  }, [sessionId]);

  // Auto-scroll to latest words
  useEffect(() => {
    if (autoScroll && streamEndRef.current) {
      streamEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [turns, provisionalWords, autoScroll]);

  const handleStop = async () => {
    setIsStopping(true);
    try {
      await stopSession(sessionId);
      setStatus('finalizing');
    } catch (err) {
      console.error(err);
      setIsStopping(false);
    }
  };

  const formatElapsed = (ms: number) => {
    const totalSecs = Math.floor(ms / 1000);
    const m = Math.floor(totalSecs / 60);
    const s = totalSecs % 60;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  return (
    <div style={{ maxWidth: 960, margin: '0 auto' }}>
      {/* Stream Status Banner */}
      <div className="glass-card" style={{ marginBottom: 20, padding: '20px 24px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
            <div className="badge badge-live">
              <span className="badge-live-pulse" />
              <span>{status.toUpperCase()}</span>
            </div>
            <div>
              <h2 style={{ fontSize: '1.25rem', color: 'white', marginBottom: 2 }}>{sessionTitle}</h2>
              <div style={{ display: 'flex', gap: 12, fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                <span>Elapsed: <strong style={{ color: 'white', fontFamily: 'var(--font-mono)' }}>{formatElapsed(elapsedMs)}</strong></span>
                <span>•</span>
                <span>Mode: <strong style={{ color: '#818cf8', textTransform: 'capitalize' }}>{processingMode}</strong></span>
              </div>
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <button
              className="btn-secondary"
              onClick={() => setAutoScroll(!autoScroll)}
              style={{ fontSize: '0.8rem', padding: '8px 14px' }}
            >
              <ArrowDown size={14} color={autoScroll ? '#10b981' : undefined} />
              <span>Auto-scroll {autoScroll ? 'ON' : 'OFF'}</span>
            </button>

            <button
              className="btn-danger"
              onClick={handleStop}
              disabled={isStopping || status === 'finalizing'}
              style={{ fontSize: '0.85rem', padding: '9px 18px' }}
            >
              <Square size={14} fill="currentColor" />
              <span>{isStopping || status === 'finalizing' ? 'Finalizing...' : 'Stop & Finalize'}</span>
            </button>
          </div>
        </div>
      </div>

      {/* Real-time Streaming Transcript Pane */}
      <div className="glass-card" style={{ minHeight: 440, display: 'flex', flexDirection: 'column' }}>
        <div className="transcript-stream-container">
          {turns.length === 0 && provisionalWords.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '80px 20px', color: 'var(--text-faint)' }}>
              <Radio size={36} style={{ margin: '0 auto 16px', opacity: 0.4 }} />
              <p style={{ fontSize: '1rem', color: 'var(--text-muted)' }}>Listening to stream...</p>
              <p style={{ fontSize: '0.85rem', marginTop: 4 }}>Words will appear within seconds of audible speech.</p>
            </div>
          ) : (
            <>
              {turns.map((turn) => (
                <div key={turn.id} className="turn-bubble">
                  <div className="turn-header">
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
                    <span className="turn-time">{formatElapsed(turn.start_ms)}</span>
                  </div>
                  <div className="turn-text">{turn.text}</div>
                </div>
              ))}

              {/* Provisional Floating Words (Revisable Tail) */}
              {provisionalWords.length > 0 && (
                <div className="turn-bubble" style={{ borderStyle: 'dashed', borderColor: 'rgba(99, 102, 241, 0.3)' }}>
                  <div className="turn-header">
                    <span className="speaker-tag" style={{ background: 'rgba(255,255,255,0.05)', color: '#94a3b8' }}>
                      <span>Provisional Tail</span>
                    </span>
                  </div>
                  <div className="turn-text word-provisional">
                    {provisionalWords.map((w) => w.text).join(' ')}
                  </div>
                </div>
              )}
            </>
          )}
          <div ref={streamEndRef} />
        </div>
      </div>
    </div>
  );
};
