'use client';

import React, { useState } from 'react';
import { X, Download, Copy, Check } from 'lucide-react';
import { getExportUrl } from '@/lib/api';

interface ExportModalProps {
  sessionId: string;
  sessionTitle: string;
  onClose: () => void;
}

type ExportFormat = 'txt' | 'md' | 'srt' | 'vtt' | 'json';

export const ExportModal: React.FC<ExportModalProps> = ({
  sessionId,
  sessionTitle,
  onClose,
}) => {
  const [format, setFormat] = useState<ExportFormat>('txt');
  const [includeTimestamps, setIncludeTimestamps] = useState(true);
  const [includeSpeakers, setIncludeSpeakers] = useState(true);
  const [copied, setCopied] = useState(false);

  const exportUrl = getExportUrl(sessionId, format, includeTimestamps, includeSpeakers);

  const handleCopy = async () => {
    try {
      const res = await fetch(exportUrl);
      const text = await res.text();
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error(err);
    }
  };

  const formats: Array<{ id: ExportFormat; label: string; desc: string }> = [
    { id: 'txt', label: 'Plain Text (.txt)', desc: 'Clean text with speaker names and timestamps' },
    { id: 'md', label: 'Markdown (.md)', desc: 'Formatted markdown document for notes or docs' },
    { id: 'srt', label: 'SubRip Captions (.srt)', desc: 'Standard subtitles for video players and YouTube' },
    { id: 'vtt', label: 'WebVTT (.vtt)', desc: 'Web caption standard with <v Speaker> voice spans' },
    { id: 'json', label: 'Structured JSON (.json)', desc: 'Lossless export with word-level timestamps & models' },
  ];

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.75)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 100,
        backdropFilter: 'blur(8px)',
        padding: 20,
      }}
    >
      <div className="glass-card" style={{ maxWidth: 520, width: '100%', padding: 28, background: '#0e131f' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
          <h2 style={{ fontSize: '1.3rem', color: 'white' }}>Export: {sessionTitle}</h2>
          <button
            onClick={onClose}
            style={{ background: 'none', border: 'none', color: '#94a3b8', cursor: 'pointer' }}
          >
            <X size={20} />
          </button>
        </div>

        {/* Format Selector */}
        <div style={{ marginBottom: 20 }}>
          <label style={{ fontSize: '0.85rem', color: 'var(--text-muted)', display: 'block', marginBottom: 10, fontWeight: 500 }}>
            Select Export Format
          </label>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {formats.map((f) => (
              <div
                key={f.id}
                onClick={() => setFormat(f.id)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: '12px 16px',
                  borderRadius: 'var(--radius-sm)',
                  border: `1px solid ${format === f.id ? 'var(--primary)' : 'var(--border-subtle)'}`,
                  background: format === f.id ? 'rgba(99, 102, 241, 0.12)' : 'rgba(255, 255, 255, 0.02)',
                  cursor: 'pointer',
                  transition: 'all 0.2s ease',
                }}
              >
                <div>
                  <div style={{ fontWeight: 600, fontSize: '0.9rem', color: format === f.id ? 'white' : 'var(--text-main)' }}>
                    {f.label}
                  </div>
                  <div style={{ fontSize: '0.78rem', color: 'var(--text-faint)' }}>{f.desc}</div>
                </div>
                <div
                  style={{
                    width: 18,
                    height: 18,
                    borderRadius: '50%',
                    border: `2px solid ${format === f.id ? 'var(--primary)' : '#475569'}`,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  {format === f.id && <div style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--primary)' }} />}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Options */}
        <div style={{ padding: '14px 0', borderTop: '1px solid var(--border-subtle)', borderBottom: '1px solid var(--border-subtle)', marginBottom: 24, display: 'flex', gap: 24 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.85rem', color: 'var(--text-main)', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={includeTimestamps}
              onChange={(e) => setIncludeTimestamps(e.target.checked)}
            />
            <span>Include Timestamps</span>
          </label>

          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.85rem', color: 'var(--text-main)', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={includeSpeakers}
              onChange={(e) => setIncludeSpeakers(e.target.checked)}
            />
            <span>Include Speaker Names</span>
          </label>
        </div>

        {/* Action Buttons */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
          <button className="btn-secondary" onClick={handleCopy}>
            {copied ? <Check size={16} color="#10b981" /> : <Copy size={16} />}
            <span>{copied ? 'Copied!' : 'Copy to Clipboard'}</span>
          </button>

          <a
            href={exportUrl}
            download
            className="btn-primary"
            style={{ textDecoration: 'none' }}
          >
            <Download size={16} />
            <span>Download File</span>
          </a>
        </div>
      </div>
    </div>
  );
};
