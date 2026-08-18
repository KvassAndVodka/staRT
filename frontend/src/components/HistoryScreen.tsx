'use client';

import React, { useState, useEffect, useCallback } from 'react';
import {
  Search, Trash2, RotateCcw, Download, Eye, HardDrive, FileText, AlertCircle, Database
} from 'lucide-react';
import { SessionSummary, StorageSummary } from '@/types';
import {
  fetchSessions, fetchTrashedSessions, trashSession, restoreSession, purgeSession, fetchStorageSummary
} from '@/lib/api';

interface HistoryScreenProps {
  onOpenSession: (sessionId: string) => void;
  onOpenExportForSession: (sessionId: string) => void;
}

export const HistoryScreen: React.FC<HistoryScreenProps> = ({
  onOpenSession,
  onOpenExportForSession,
}) => {
  const [tab, setTab] = useState<'active' | 'trash'>('active');
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [trashedSessions, setTrashedSessions] = useState<SessionSummary[]>([]);
  const [storage, setStorage] = useState<StorageSummary | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [isLoading, setIsLoading] = useState(true);

  const loadData = useCallback(async () => {
    setIsLoading(true);
    try {
      const [activeData, trashData, storageData] = await Promise.all([
        fetchSessions(),
        fetchTrashedSessions(),
        fetchStorageSummary(),
      ]);
      setSessions(activeData);
      setTrashedSessions(trashData);
      setStorage(storageData);
    } catch (err) {
      console.error(err);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    let isMounted = true;
    Promise.all([
      fetchSessions(),
      fetchTrashedSessions(),
      fetchStorageSummary(),
    ]).then(([activeData, trashData, storageData]) => {
      if (isMounted) {
        setSessions(activeData);
        setTrashedSessions(trashData);
        setStorage(storageData);
        setIsLoading(false);
      }
    }).catch((err) => {
      console.error(err);
      if (isMounted) setIsLoading(false);
    });

    return () => {
      isMounted = false;
    };
  }, []);

  const handleTrash = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await trashSession(id);
      await loadData();
    } catch (err) {
      console.error(err);
    }
  };

  const handleRestore = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await restoreSession(id);
      await loadData();
    } catch (err) {
      console.error(err);
    }
  };

  const handlePurge = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!window.confirm('Permanently purge this session and all its audio fragments? This action cannot be undone.')) {
      return;
    }
    try {
      await purgeSession(id);
      await loadData();
    } catch (err) {
      console.error(err);
    }
  };

  const formatBytes = (bytes: number) => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  const formatDuration = (ms?: number | null) => {
    if (!ms) return '--:--';
    const totalSecs = Math.floor(ms / 1000);
    const m = Math.floor(totalSecs / 60);
    const s = totalSecs % 60;
    return `${m}m ${s}s`;
  };

  const currentList = tab === 'active' ? sessions : trashedSessions;
  const filteredList = currentList.filter((s) =>
    s.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
    s.source_url.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto' }}>
      {/* Storage Summary Metric Cards */}
      {storage && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 16, marginBottom: 28 }}>
          <div className="glass-card" style={{ padding: '18px 22px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: 6 }}>
              <FileText size={16} color="#818cf8" />
              <span>Active Transcripts</span>
            </div>
            <div style={{ fontSize: '1.6rem', fontWeight: 700, color: 'white' }}>
              {storage.active_sessions}
            </div>
          </div>

          <div className="glass-card" style={{ padding: '18px 22px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: 6 }}>
              <HardDrive size={16} color="#06b6d4" />
              <span>Local Audio Storage</span>
            </div>
            <div style={{ fontSize: '1.6rem', fontWeight: 700, color: 'white' }}>
              {formatBytes(storage.total_audio_bytes)}
            </div>
          </div>

          <div className="glass-card" style={{ padding: '18px 22px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: 6 }}>
              <Database size={16} color="#10b981" />
              <span>Exports Generated</span>
            </div>
            <div style={{ fontSize: '1.6rem', fontWeight: 700, color: 'white' }}>
              {formatBytes(storage.total_export_bytes)}
            </div>
          </div>

          <div className="glass-card" style={{ padding: '18px 22px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: 6 }}>
              <Trash2 size={16} color="#f43f5e" />
              <span>Items in Trash</span>
            </div>
            <div style={{ fontSize: '1.6rem', fontWeight: 700, color: 'white' }}>
              {storage.trashed_sessions}
            </div>
          </div>
        </div>
      )}

      {/* History Header & Controls */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20, flexWrap: 'wrap', gap: 16 }}>
        {/* Tab Toggle */}
        <div className="nav-tabs" style={{ background: 'rgba(255,255,255,0.03)' }}>
          <button
            className={`nav-tab ${tab === 'active' ? 'active' : ''}`}
            onClick={() => setTab('active')}
          >
            <span>Active Sessions ({sessions.length})</span>
          </button>
          <button
            className={`nav-tab ${tab === 'trash' ? 'active' : ''}`}
            onClick={() => setTab('trash')}
          >
            <Trash2 size={14} />
            <span>Trash ({trashedSessions.length})</span>
          </button>
        </div>

        {/* Search Field */}
        <div style={{ position: 'relative', width: 320 }}>
          <Search size={16} style={{ position: 'absolute', left: 14, top: '50%', transform: 'translateY(-50%)', color: '#64748b' }} />
          <input
            type="text"
            className="input-field"
            style={{ padding: '10px 14px 10px 38px', fontSize: '0.9rem' }}
            placeholder="Search transcripts by title or URL..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
      </div>

      {/* Sessions Table / Cards */}
      {isLoading ? (
        <div style={{ textAlign: 'center', padding: '60px 20px', color: 'var(--text-muted)' }}>
          <p>Loading history records...</p>
        </div>
      ) : filteredList.length === 0 ? (
        <div className="glass-card" style={{ textAlign: 'center', padding: '60px 20px', color: 'var(--text-muted)' }}>
          <AlertCircle size={32} style={{ margin: '0 auto 12px', opacity: 0.5 }} />
          <p style={{ fontSize: '1rem', color: 'var(--text-main)' }}>No sessions found.</p>
          <p style={{ fontSize: '0.85rem', marginTop: 4 }}>
            {tab === 'trash' ? 'Trash is empty.' : 'Start a new transcription session from the home screen.'}
          </p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {filteredList.map((s) => (
            <div
              key={s.id}
              className="glass-card"
              style={{
                padding: '18px 24px',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                flexWrap: 'wrap',
                gap: 16,
                cursor: tab === 'active' ? 'pointer' : 'default',
              }}
              onClick={() => tab === 'active' && onOpenSession(s.id)}
            >
              <div style={{ maxWidth: 580 }}>
                <div style={{ fontSize: '1.05rem', fontWeight: 600, color: 'white', marginBottom: 6 }}>
                  {s.title}
                </div>
                <div style={{ display: 'flex', gap: 12, fontSize: '0.8rem', color: 'var(--text-muted)', flexWrap: 'wrap' }}>
                  <span>{new Date(s.created_at).toLocaleString()}</span>
                  <span>•</span>
                  <span>Duration: <strong>{formatDuration(s.duration_ms)}</strong></span>
                  <span>•</span>
                  <span>Language: <strong style={{ textTransform: 'capitalize' }}>{s.language_mode}</strong></span>
                  <span>•</span>
                  <span>Speakers: <strong>{s.speaker_count}</strong></span>
                </div>
              </div>

              {/* Action Buttons */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                {tab === 'active' ? (
                  <>
                    <button
                      className="btn-secondary"
                      onClick={(e) => {
                        e.stopPropagation();
                        onOpenSession(s.id);
                      }}
                      style={{ padding: '8px 14px', fontSize: '0.85rem' }}
                    >
                      <Eye size={15} />
                      <span>Review</span>
                    </button>

                    <button
                      className="btn-secondary"
                      onClick={(e) => {
                        e.stopPropagation();
                        onOpenExportForSession(s.id);
                      }}
                      style={{ padding: '8px 14px', fontSize: '0.85rem' }}
                    >
                      <Download size={15} />
                      <span>Export</span>
                    </button>

                    <button
                      className="btn-secondary"
                      onClick={(e) => handleTrash(s.id, e)}
                      style={{ padding: '8px 12px', color: '#fda4af' }}
                      title="Move to Trash"
                    >
                      <Trash2 size={15} />
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      className="btn-secondary"
                      onClick={(e) => handleRestore(s.id, e)}
                      style={{ padding: '8px 14px', fontSize: '0.85rem' }}
                    >
                      <RotateCcw size={15} />
                      <span>Restore</span>
                    </button>

                    <button
                      className="btn-danger"
                      onClick={(e) => handlePurge(s.id, e)}
                      style={{ padding: '8px 14px', fontSize: '0.85rem' }}
                    >
                      <Trash2 size={15} />
                      <span>Purge Permanently</span>
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
