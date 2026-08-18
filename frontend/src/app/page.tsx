'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Header } from '@/components/Header';
import { HomeScreen } from '@/components/HomeScreen';
import { LiveScreen } from '@/components/LiveScreen';
import { ReviewStudio } from '@/components/ReviewStudio';
import { HistoryScreen } from '@/components/HistoryScreen';
import { ExportModal } from '@/components/ExportModal';
import { SessionSummary, HealthInfo } from '@/types';
import { fetchSessions, fetchHealth } from '@/lib/api';

export default function Home() {
  const [activeTab, setActiveTab] = useState<'home' | 'live' | 'review' | 'history'>('home');
  const [activeLiveSessionId, setActiveLiveSessionId] = useState<string | null>(null);
  const [selectedReviewSessionId, setSelectedReviewSessionId] = useState<string | null>(null);
  const [exportSessionId, setExportSessionId] = useState<string | null>(null);

  const [recentSessions, setRecentSessions] = useState<SessionSummary[]>([]);
  const [health, setHealth] = useState<HealthInfo | null>(null);

  const loadInitialData = useCallback(async () => {
    try {
      const [sessionsData, healthData] = await Promise.all([
        fetchSessions(),
        fetchHealth().catch(() => null),
      ]);
      setRecentSessions(sessionsData);
      setHealth(healthData);

      const live = sessionsData.find((s) => s.status === 'live' || s.status === 'connecting');
      if (live) {
        setActiveLiveSessionId(live.id);
      }
    } catch (err) {
      console.error('[staRT] Initial data load error:', err);
    }
  }, []);

  useEffect(() => {
    let isMounted = true;
    Promise.all([
      fetchSessions(),
      fetchHealth().catch(() => null),
    ]).then(([sessionsData, healthData]) => {
      if (isMounted) {
        setRecentSessions(sessionsData);
        setHealth(healthData);
        const live = sessionsData.find((s) => s.status === 'live' || s.status === 'connecting');
        if (live) {
          setActiveLiveSessionId(live.id);
        }
      }
    }).catch((err) => console.error(err));

    return () => {
      isMounted = false;
    };
  }, []);

  const handleSessionStarted = (sessionId: string) => {
    setActiveLiveSessionId(sessionId);
    setSelectedReviewSessionId(sessionId);
    setActiveTab('live');
    loadInitialData();
  };

  const handleLiveFinalized = (sessionId: string) => {
    setActiveLiveSessionId(null);
    setSelectedReviewSessionId(sessionId);
    setActiveTab('review');
    loadInitialData();
  };

  const handleOpenReviewSession = (sessionId: string) => {
    setSelectedReviewSessionId(sessionId);
    setActiveTab('review');
  };

  const handleOpenExport = (sessionId?: string) => {
    const target = sessionId || selectedReviewSessionId || (recentSessions.length ? recentSessions[0].id : null);
    if (target) {
      setExportSessionId(target);
    }
  };

  return (
    <div className="app-container">
      <Header
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        activeSessionId={activeLiveSessionId}
        health={health}
      />

      <main>
        {activeTab === 'home' && (
          <HomeScreen
            recentSessions={recentSessions}
            onSessionStarted={handleSessionStarted}
            onOpenSession={handleOpenReviewSession}
          />
        )}

        {activeTab === 'live' && activeLiveSessionId && (
          <LiveScreen
            sessionId={activeLiveSessionId}
            onFinalized={handleLiveFinalized}
          />
        )}

        {activeTab === 'review' && (
          selectedReviewSessionId ? (
            <ReviewStudio
              sessionId={selectedReviewSessionId}
              onOpenExport={() => handleOpenExport(selectedReviewSessionId)}
            />
          ) : recentSessions.length > 0 ? (
            <ReviewStudio
              sessionId={recentSessions[0].id}
              onOpenExport={() => handleOpenExport(recentSessions[0].id)}
            />
          ) : (
            <div style={{ textAlign: 'center', padding: '100px 20px', color: 'var(--text-muted)' }}>
              <p>No transcripts recorded yet. Start a session from the Home screen.</p>
            </div>
          )
        )}

        {activeTab === 'history' && (
          <HistoryScreen
            onOpenSession={handleOpenReviewSession}
            onOpenExportForSession={handleOpenExport}
          />
        )}
      </main>

      {/* Export Modal */}
      {exportSessionId && (
        <ExportModal
          sessionId={exportSessionId}
          sessionTitle="Export Transcript"
          onClose={() => setExportSessionId(null)}
        />
      )}
    </div>
  );
}
