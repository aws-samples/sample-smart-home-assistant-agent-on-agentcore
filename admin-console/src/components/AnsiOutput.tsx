/**
 * Terminal-style output pane for the Remote Shell modal.
 *
 * Accepts an append-only array of ShellChunk events. Renders stdout with
 * ANSI color codes (limited subset: reset, bold, 30-37, 90-97); stderr
 * always gets a red class so admins can eyeball interleaved error output.
 * Auto-scrolls to bottom unless the user has scrolled up (stickiness at
 * the very bottom uses a 20-px tolerance).
 *
 * Drops the oldest chunks when accumulated output exceeds `capBytes`
 * (default 5 MB), prepending a "[... N KB truncated ...]" banner.
 */
import React, { useEffect, useMemo, useRef } from 'react';
import type { ShellChunk } from '../api/agentcoreCommand';

const DEFAULT_CAP_BYTES = 5 * 1024 * 1024;
const TRIM_TARGET_FRACTION = 0.8;

const ANSI_TO_CLASS: Record<string, string> = {
  '0': 'ansi-reset',
  '1': 'ansi-bold',
  '30': 'ansi-fg-black',
  '31': 'ansi-fg-red',
  '32': 'ansi-fg-green',
  '33': 'ansi-fg-yellow',
  '34': 'ansi-fg-blue',
  '35': 'ansi-fg-magenta',
  '36': 'ansi-fg-cyan',
  '37': 'ansi-fg-white',
  '90': 'ansi-fg-bright-black',
  '91': 'ansi-fg-bright-red',
  '92': 'ansi-fg-bright-green',
  '93': 'ansi-fg-bright-yellow',
  '94': 'ansi-fg-bright-blue',
  '95': 'ansi-fg-bright-magenta',
  '96': 'ansi-fg-bright-cyan',
  '97': 'ansi-fg-bright-white',
};

const ANSI_SGR_RE = /\[([0-9;]*)m/g;
const ANSI_ANY_RE = /\[[0-9;?]*[A-Za-z]/g;

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

/**
 * Convert a single plain-text chunk (stdout or stderr) to an HTML fragment.
 * Unknown ANSI sequences (cursor moves, 256-color, etc.) are stripped silently.
 */
function ansiToHtml(text: string, stderr: boolean): string {
  // Strip non-SGR escape sequences first (cursor moves, clear-line, etc.)
  const stripped = text.replace(ANSI_ANY_RE, (m) =>
    m.endsWith('m') ? m : '',
  );

  let html = '';
  let lastIndex = 0;
  let openSpans: string[] = [];

  const flushOpen = () => {
    html += '</span>'.repeat(openSpans.length);
    openSpans = [];
  };

  const applySgr = (paramsRaw: string) => {
    const params = paramsRaw === '' ? ['0'] : paramsRaw.split(';');
    for (const p of params) {
      if (p === '0' || p === '') {
        flushOpen();
        continue;
      }
      const cls = ANSI_TO_CLASS[p];
      if (cls) {
        html += `<span class="${cls}">`;
        openSpans.push(cls);
      }
    }
  };

  ANSI_SGR_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = ANSI_SGR_RE.exec(stripped)) !== null) {
    html += escapeHtml(stripped.slice(lastIndex, match.index));
    applySgr(match[1]);
    lastIndex = match.index + match[0].length;
  }
  html += escapeHtml(stripped.slice(lastIndex));
  flushOpen();

  if (stderr) {
    html = `<span class="shell-stderr">${html}</span>`;
  }
  return html;
}

export interface AnsiOutputProps {
  chunks: ShellChunk[];
  capBytes?: number;
}

const AnsiOutput: React.FC<AnsiOutputProps> = ({ chunks, capBytes = DEFAULT_CAP_BYTES }) => {
  const preRef = useRef<HTMLPreElement | null>(null);
  const stickToBottomRef = useRef<boolean>(true);

  const { html } = useMemo(() => {
    let totalBytes = 0;
    const kept: { text: string; stderr: boolean }[] = [];
    for (const c of chunks) {
      if (c.kind !== 'stdout' && c.kind !== 'stderr') continue;
      const bytes = new TextEncoder().encode(c.text).length;
      totalBytes += bytes;
      kept.push({ text: c.text, stderr: c.kind === 'stderr' });
    }

    let truncBytes = 0;
    if (totalBytes > capBytes) {
      const target = Math.floor(capBytes * TRIM_TARGET_FRACTION);
      while (kept.length > 0 && totalBytes > target) {
        const removed = kept.shift()!;
        const removedBytes = new TextEncoder().encode(removed.text).length;
        totalBytes -= removedBytes;
        truncBytes += removedBytes;
      }
    }

    let out = '';
    if (truncBytes > 0) {
      const kb = Math.round(truncBytes / 1024);
      out += `<span class="shell-truncated">[… ${kb} KB truncated …]\n</span>`;
    }
    for (const k of kept) out += ansiToHtml(k.text, k.stderr);
    return { html: out };
  }, [chunks, capBytes]);

  useEffect(() => {
    const el = preRef.current;
    if (!el) return;
    if (stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [html]);

  const onScroll = () => {
    const el = preRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - (el.scrollTop + el.clientHeight) < 20;
    stickToBottomRef.current = atBottom;
  };

  return (
    <pre
      ref={preRef}
      className="shell-output-pane"
      onScroll={onScroll}
      // eslint-disable-next-line react/no-danger
      dangerouslySetInnerHTML={{ __html: html }}
      aria-label="Remote shell output"
    />
  );
};

export default AnsiOutput;

// Exported for the Copy button — strips ANSI escapes for clipboard output.
export function chunksToPlainText(chunks: ShellChunk[]): string {
  let s = '';
  for (const c of chunks) {
    if (c.kind === 'stdout' || c.kind === 'stderr') s += c.text;
  }
  return s.replace(ANSI_ANY_RE, '');
}
