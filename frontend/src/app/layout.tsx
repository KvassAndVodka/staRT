import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'staRT — Local Transcript Service',
  description: 'Local-first, zero-cost live transcription and multi-speaker diarization for public media and streams.',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
