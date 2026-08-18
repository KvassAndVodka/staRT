'use client';

import React from 'react';
import { Activity, Radio, History, Layers, Zap } from 'lucide-react';
import { HealthInfo } from '@/types';

interface HeaderProps {
  activeTab: 'home' | 'live' | 'review' | 'history';
  setActiveTab: (tab: 'home' | 'live' | 'review' | 'history') => void;
  activeSessionId: string | null;
  health: HealthInfo | null;
}

export const Header: React.FC<HeaderProps> = ({
  activeTab,
  setActiveTab,
  activeSessionId,
  health,
}) => {
  return (
    <header className="app-header">
      <div className="brand-container" onClick={() => setActiveTab('home')}>
        <div className="brand-icon">
          <Activity size={22} />
        </div>
        <div>
          <span className="brand-title">sta<span style={{ color: '#818cf8' }}>RT</span></span>
          <span style={{ fontSize: '0.75rem', color: '#64748b', marginLeft: '8px', fontWeight: 500 }}>
            Local Transcript Service
          </span>
        </div>
      </div>

      <nav className="nav-tabs">
        <button
          className={`nav-tab ${activeTab === 'home' ? 'active' : ''}`}
          onClick={() => setActiveTab('home')}
        >
          <Zap size={16} />
          <span>New Session</span>
        </button>

        {activeSessionId && (
          <button
            className={`nav-tab ${activeTab === 'live' ? 'active' : ''}`}
            onClick={() => setActiveTab('live')}
          >
            <Radio size={16} color="#f43f5e" />
            <span>Live Stream</span>
            <span className="badge-live-pulse" />
          </button>
        )}

        <button
          className={`nav-tab ${activeTab === 'review' ? 'active' : ''}`}
          onClick={() => setActiveTab('review')}
        >
          <Layers size={16} />
          <span>Review Studio</span>
        </button>

        <button
          className={`nav-tab ${activeTab === 'history' ? 'active' : ''}`}
          onClick={() => setActiveTab('history')}
        >
          <History size={16} />
          <span>History & Trash</span>
        </button>
      </nav>

      <div>
        {health ? (
          <div className="badge badge-cuda">
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#10b981' }} />
            <span>
              {health.cuda_devices > 0 ? 'CUDA GPU' : 'CPU'}: {health.default_model} ({health.default_compute_type})
            </span>
          </div>
        ) : (
          <div className="badge" style={{ background: 'rgba(255,255,255,0.05)', color: '#94a3b8' }}>
            <span>Connecting...</span>
          </div>
        )}
      </div>
    </header>
  );
};
