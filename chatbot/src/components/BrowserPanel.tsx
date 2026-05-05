/**
 * Right-side panel that shows the running AgentCore browser session as a
 * live DCV stream plus a file browser for the text-agent runtime's
 * /mnt/workspace/<sessionId>/ (NOT the browser sandbox filesystem — the
 * agent session workspace, same one remote-shell lists).
 *
 * The live view uses the Amazon DCV Web Client SDK (loaded from
 * /dcvjs/dcv.js on the chatbot origin). The presigned URL from
 * BrowserClient.generate_live_view_url() is SigV4-signed; DCV consumes
 * its auth query-string via the httpExtraSearchParams callback.
 */
import React, { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import Tabs from '@cloudscape-design/components/tabs';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Header from '@cloudscape-design/components/header';
import { useI18n } from '../i18n';
import { BrowserSessionInfo } from '../api/browserSessions';
import { listWorkspace, fetchWorkspaceFile, WorkspaceEntry } from '../api/workspaceFiles';
import { takeControl, releaseControl } from '../api/browserControl';

interface Props {
  session: BrowserSessionInfo | null;
  agentSessionId: string | null;
  // Expanded state lives in the parent so the panel can reopen on a
  // specific tab without remounting the file-browser / DCV viewer.
  expanded: boolean;
  activeTab: 'live' | 'files';
  onExpand: (tab: 'live' | 'files') => void;
  onCollapse: () => void;
}

// Collapsed-state tab rail: two vertical labels on the right edge that,
// when clicked, expand the panel with the chosen tab active. Returning
// `null` from onClose() dismisses the panel entirely (different behavior
// from collapsing — see ChatInterface state split between open/tab).
interface RailProps {
  t: (k: string) => string;
  onOpen: (tab: 'live' | 'files') => void;
}

const CollapsedRail: React.FC<RailProps> = ({ t, onOpen }) => (
  <div
    style={{
      width: 40,
      borderLeft: '1px solid #e0e0e0',
      background: 'var(--color-background-container-content, #fff)',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'stretch',
    }}
  >
    {([
      { id: 'live', label: t('browserPanel.title') },
      { id: 'files', label: t('browserPanel.files') },
    ] as const).map(({ id, label }) => (
      <button
        key={id}
        onClick={() => onOpen(id)}
        title={label}
        style={{
          // Bottom-to-top reading direction (standard for right-edge tab
          // rails — IDE sidebars etc.). The previous `vertical-rl +
          // rotate(180deg)` combination printed labels upside-down.
          writingMode: 'vertical-rl',
          flex: 1,
          border: 'none',
          background: 'transparent',
          padding: '16px 4px',
          cursor: 'pointer',
          fontSize: 14,
          color: 'var(--color-text-body-default, #16191f)',
          borderBottom: id === 'live' ? '1px solid #e0e0e0' : 'none',
        }}
      >
        {label}
      </button>
    ))}
  </div>
);

const WORKSPACE_ROOT = '/mnt/workspace';
const DCV_DIV_ID = 'dcv-display-smarthome';

declare global {
  interface Window {
    dcv?: any;
  }
}

let dcvLoadingPromise: Promise<void> | null = null;

function ensureDcvLoaded(): Promise<void> {
  if (typeof window.dcv !== 'undefined') return Promise.resolve();
  if (dcvLoadingPromise) return dcvLoadingPromise;
  dcvLoadingPromise = new Promise<void>((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/dcvjs/dcv.js';
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('Failed to load /dcvjs/dcv.js'));
    document.head.appendChild(s);
  });
  return dcvLoadingPromise;
}

interface DcvViewerProps {
  presignedUrl: string;
}

// Canvas resolution the DCV server will stream. Matches the viewport we
// pass to start_browser_session in tools/browser_use.py so horizontal
// scrolling on common sites (Amazon, Wikipedia) is rare — but when it
// happens the scrollWrapper's overflow:auto gives us native scrollbars.
const DCV_CANVAS_WIDTH = 1280;
const DCV_CANVAS_HEIGHT = 800;

const DcvViewer: React.FC<DcvViewerProps> = ({ presignedUrl }) => {
  const [status, setStatus] = useState<'loading' | 'connecting' | 'connected' | 'error'>('loading');
  const [error, setError] = useState('');
  const connectionRef = useRef<any>(null);
  const layoutRequestedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    layoutRequestedRef.current = false;

    // Ask the DCV server to render at our fixed canvas size and size the
    // local <div> to match. Called on firstFrame and displayLayout. Without
    // this the server picks a small default (often 900×800) that gets
    // letterboxed into a blurry scaled bitmap instead of a scrollable
    // crisp render.
    const requestLayout = () => {
      if (layoutRequestedRef.current) return;
      const conn = connectionRef.current;
      if (!conn) return;
      try {
        conn.requestDisplayLayout([{
          name: 'Main Display',
          rect: { x: 0, y: 0, width: DCV_CANVAS_WIDTH, height: DCV_CANVAS_HEIGHT },
          primary: true,
        }]);
        layoutRequestedRef.current = true;
      } catch {
        // DCV swallows some internal errors; the next callback will retry.
      }
    };

    (async () => {
      try {
        await ensureDcvLoaded();
        if (cancelled) return;
        const dcv = window.dcv;
        if (!dcv) throw new Error('DCV SDK unavailable');

        const httpExtraSearchParams = () => new URL(presignedUrl).searchParams;

        setStatus('connecting');
        dcv.authenticate(presignedUrl, {
          promptCredentials: () => { /* presigned — no prompt */ },
          error: (_a: any, err: any) => {
            if (cancelled) return;
            setError(err?.message || String(err));
            setStatus('error');
          },
          success: (_a: any, result: any) => {
            if (cancelled) return;
            const row = result && result[0];
            if (!row) {
              setError('no session in auth result');
              setStatus('error');
              return;
            }
            dcv.connect({
              url: presignedUrl,
              sessionId: row.sessionId,
              authToken: row.authToken,
              divId: DCV_DIV_ID,
              baseUrl: '/dcvjs',
              callbacks: {
                firstFrame: () => {
                  if (cancelled) return;
                  setStatus('connected');
                  requestLayout();
                },
                error: (err: any) => {
                  if (cancelled) return;
                  setError(err?.message || String(err));
                  setStatus('error');
                },
                httpExtraSearchParams,
                displayLayout: () => { requestLayout(); },
              },
            }).then((conn: any) => { connectionRef.current = conn; requestLayout(); })
              .catch((err: any) => { if (!cancelled) { setError(err?.message || String(err)); setStatus('error'); } });
          },
          httpExtraSearchParams,
        });
      } catch (e: any) {
        if (!cancelled) { setError(e?.message || String(e)); setStatus('error'); }
      }
    })();
    return () => {
      cancelled = true;
      try { connectionRef.current?.disconnect?.(); } catch { /* ignore */ }
      connectionRef.current = null;
    };
  }, [presignedUrl]);

  return (
    <div
      style={{
        position: 'relative',
        // Scroll wrapper: DCV renders at a fixed canvas size (see
        // DCV_CANVAS_*), and this div scrolls when the panel is
        // narrower/shorter than the canvas — both x and y.
        width: '100%',
        height: 'calc(100vh - 200px)',
        minHeight: 400,
        overflow: 'auto',
        background: '#000',
        borderRadius: 4,
      }}
    >
      <div
        id={DCV_DIV_ID}
        style={{
          // Fixed pixel size so the scroll wrapper sees a concrete content
          // rect. The DCV SDK will paint into this div at the resolution
          // we requested via requestDisplayLayout.
          width: DCV_CANVAS_WIDTH,
          height: DCV_CANVAS_HEIGHT,
          background: '#000',
        }}
      />
      {status !== 'connected' && (
        <div style={{
          position: 'absolute', inset: 0, display: 'flex', alignItems: 'center',
          justifyContent: 'center', color: '#fff', pointerEvents: 'none',
        }}>
          {status === 'error' ? `Live view error: ${error}` : 'Connecting to live browser…'}
        </div>
      )}
    </div>
  );
};

const BrowserPanel: React.FC<Props> = ({ session, agentSessionId, expanded, activeTab, onExpand, onCollapse }) => {
  const { t } = useI18n();
  const initialPath = useMemo(
    () => (agentSessionId ? `${WORKSPACE_ROOT}/${agentSessionId}` : WORKSPACE_ROOT),
    [agentSessionId],
  );
  const [path, setPath] = useState(initialPath);
  const [entries, setEntries] = useState<WorkspaceEntry[] | null>(null);
  const [filesError, setFilesError] = useState('');

  // Whether the panel is in its default expanded width (520px) or
  // maximised to fill the chat column. Users reliably hit this from the
  // header toolbar when Amazon/Wikipedia content scrolls horizontally.
  const [maximized, setMaximized] = useState(false);
  // Control mode: true = human has control (agent automation DISABLED),
  // false = agent has control (the default state inside browse_web). The
  // toggle resets whenever we see a new sessionId so stale state from a
  // previous run doesn't confuse the button label.
  const [humanControl, setHumanControl] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const [controlError, setControlError] = useState('');
  useEffect(() => {
    setHumanControl(false);
    setControlError('');
  }, [session?.sessionId]);

  const onToggleControl = useCallback(async () => {
    if (!session?.sessionId) return;
    setControlBusy(true);
    setControlError('');
    try {
      if (humanControl) {
        await releaseControl(session.sessionId, session.browserIdentifier);
        setHumanControl(false);
      } else {
        await takeControl(session.sessionId, session.browserIdentifier);
        setHumanControl(true);
      }
    } catch (e: any) {
      setControlError(e?.message ?? String(e));
    } finally {
      setControlBusy(false);
    }
  }, [humanControl, session]);

  useEffect(() => { setPath(initialPath); }, [initialPath]);

  const loadFiles = useCallback(async () => {
    if (!agentSessionId) return;
    setFilesError('');
    try {
      const r = await listWorkspace(agentSessionId, path);
      setEntries(r.entries);
    } catch (e: any) {
      setFilesError(e?.message ?? t('browserPanel.errorLoad'));
      setEntries([]);
    }
  }, [agentSessionId, path, t]);

  useEffect(() => { loadFiles(); }, [loadFiles]);

  const onDescend = (name: string) => setPath((p) => `${p.replace(/\/$/, '')}/${name}`);
  const onUp = () => {
    const parent = path.replace(/\/[^/]+$/, '');
    if (parent.length >= WORKSPACE_ROOT.length) setPath(parent);
  };
  const onDownload = async (name: string) => {
    if (!agentSessionId) return;
    try {
      const file = await fetchWorkspaceFile(agentSessionId, `${path}/${name}`);
      const bin = Uint8Array.from(atob(file.base64), (c) => c.charCodeAt(0));
      const blob = new Blob([bin], { type: file.mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = name; a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      setFilesError(e?.message ?? t('browserPanel.errorLoad'));
    }
  };

  // DCV URLs are pre-signed per session and AgentCore stops the Chrome
  // container the moment the browse_web tool returns. If we render the
  // viewer for a completed row, DCV attempts to attach and fails with
  // "Failed to communicate with server". Only mount the viewer while the
  // session is still running; once it flips to completed/failed we show
  // a simple "session ended" note so stale-URL errors don't mislead.
  const liveView = !session?.liveViewUrl ? (
    <Box textAlign="center" color="text-status-inactive" padding={{ vertical: 'xl' }}>
      {t('browserPanel.noActive')}
    </Box>
  ) : session.status === 'running' || session.status === 'idle' ? (
    <SpaceBetween size="s">
      <Box variant="p">
        <strong>{t('browserPanel.goal')}:</strong> {session.goal}
      </Box>
      <SpaceBetween direction="horizontal" size="xs">
        <Button
          iconName={humanControl ? 'angle-left-double' : 'angle-right-double'}
          loading={controlBusy}
          onClick={onToggleControl}
        >
          {humanControl ? t('browserPanel.releaseControl') : t('browserPanel.takeControl')}
        </Button>
        {humanControl && (
          <Box color="text-status-info" fontSize="body-s">
            {t('browserPanel.humanMode')}
          </Box>
        )}
        {session.status === 'idle' && !humanControl && (
          <Box color="text-status-info" fontSize="body-s">
            {t('browserPanel.idleMode')}
          </Box>
        )}
      </SpaceBetween>
      {controlError && <StatusIndicator type="error">{controlError}</StatusIndicator>}
      <DcvViewer key={session.sessionId} presignedUrl={session.liveViewUrl} />
    </SpaceBetween>
  ) : (
    <SpaceBetween size="s">
      <Box variant="p">
        <strong>{t('browserPanel.goal')}:</strong> {session.goal}
      </Box>
      <Box textAlign="center" color="text-status-inactive" padding={{ vertical: 'xl' }}>
        {t('browserPanel.sessionEnded')}
      </Box>
    </SpaceBetween>
  );

  const filesTab = (
    <SpaceBetween size="s">
      <SpaceBetween direction="horizontal" size="xs">
        <Button iconName="angle-up" onClick={onUp} disabled={path === WORKSPACE_ROOT}>
          {t('browserPanel.parent')}
        </Button>
        <Button iconName="refresh" onClick={loadFiles}>
          {t('browserPanel.refresh')}
        </Button>
      </SpaceBetween>
      <Box variant="code" fontSize="body-s">{path}</Box>
      {filesError && <StatusIndicator type="error">{filesError}</StatusIndicator>}
      {entries && entries.length === 0 && !filesError && <Box>{t('browserPanel.emptyDir')}</Box>}
      <ul style={{ listStyle: 'none', padding: 0, margin: 0, maxHeight: '50vh', overflowY: 'auto' }}>
        {entries?.map((e) => (
          <li key={e.name} style={{ padding: '4px 0' }}>
            {e.isDir ? (
              <Button variant="inline-link" onClick={() => onDescend(e.name)}>
                {e.name}/
              </Button>
            ) : (
              <Button variant="inline-link" onClick={() => onDownload(e.name)}>
                {e.name} ({e.size}B)
              </Button>
            )}
          </li>
        ))}
      </ul>
    </SpaceBetween>
  );

  if (!expanded) {
    return <CollapsedRail t={t} onOpen={onExpand} />;
  }

  return (
    <div
      style={{
        // Maximised: take all remaining horizontal space (flex: 1 in the
        // parent row). Default: a readable 720px (wider than the initial
        // 520px so Amazon/Wikipedia render without horizontal overflow).
        ...(maximized
          ? { flex: 1, width: 'auto', minWidth: 0 }
          : { width: 720, minWidth: 520 }),
        borderLeft: '1px solid #e0e0e0',
        background: 'var(--color-background-container-content, #fff)',
        padding: 16,
        overflowY: 'auto',
      }}
    >
      <Header
        variant="h3"
        actions={
          <SpaceBetween direction="horizontal" size="xxs">
            <Button
              iconName={maximized ? 'shrink' : 'expand'}
              variant="icon"
              onClick={() => setMaximized((m) => !m)}
              ariaLabel={maximized ? t('browserPanel.restore') : t('browserPanel.maximize')}
            />
            {/* The ✕ collapses the panel back to the vertical rail (it does
                not dismiss it entirely — the rail is always present so the
                user can re-expand on demand). */}
            <Button
              iconName="close"
              variant="icon"
              onClick={onCollapse}
              ariaLabel={t('browserPanel.collapse')}
            />
          </SpaceBetween>
        }
      >
        {t('browserPanel.title')}
      </Header>
      <div style={{ marginTop: 12 }}>
        <Tabs
          activeTabId={activeTab}
          onChange={({ detail }) => onExpand(detail.activeTabId as 'live' | 'files')}
          tabs={[
            { id: 'live', label: t('browserPanel.liveView'), content: liveView },
            { id: 'files', label: t('browserPanel.files'), content: filesTab },
          ]}
        />
      </div>
    </div>
  );
};

export default BrowserPanel;
