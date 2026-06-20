/**
 * CodeInterpreter tab content — renders an AgentCore Code Interpreter run
 * step by step: the executed Python (syntax-highlighted), its streaming
 * stdout/stderr, and any charts the code saved, loaded inline.
 *
 * Charts live in the text agent's session workspace
 * (/mnt/workspace/<agentSessionId>/code/...), the same place browse_web saves
 * screenshots, and are fetched via InvokeAgentRuntimeCommand (workspaceFiles)
 * — no new infrastructure.
 */
import React, { useEffect, useState, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Spinner from '@cloudscape-design/components/spinner';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { useI18n } from '../i18n';
import { CodeSessionInfo, CodeStep } from '../api/codeSessions';
import { fetchWorkspaceFile } from '../api/workspaceFiles';

const WORKSPACE_ROOT = '/mnt/workspace';

// Loads one chart from the session workspace and renders it as an inline
// image. Keyed by (agentSessionId, relPath) so a step re-render doesn't
// re-fetch a chart that's already resolved.
const ChartImage: React.FC<{ agentSessionId: string | null; relPath: string }> = ({
  agentSessionId,
  relPath,
}) => {
  const [src, setSrc] = useState('');
  const [err, setErr] = useState('');

  useEffect(() => {
    if (!agentSessionId) return;
    let cancelled = false;
    // The chart is written into /mnt/workspace by the agent invocation, but
    // we read it from a SEPARATE InvokeAgentRuntimeCommand microVM. Session
    // storage isn't always synced to the read side at the instant the DDB row
    // (with charts[]) becomes visible to our poll, so a single fetch can race
    // the write and return "not a regular file". Retry with backoff — the file
    // reliably appears within a few seconds — and only surface an error once
    // every attempt has been exhausted.
    const delaysMs = [0, 1000, 2000, 3000, 5000];
    (async () => {
      const full = `${WORKSPACE_ROOT}/${agentSessionId}/${relPath}`;
      let lastErr = 'chart load failed';
      for (let i = 0; i < delaysMs.length; i++) {
        if (delaysMs[i] > 0) {
          await new Promise((r) => setTimeout(r, delaysMs[i]));
        }
        if (cancelled) return;
        try {
          const file = await fetchWorkspaceFile(agentSessionId, full);
          if (cancelled) return;
          setSrc(`data:${file.mime};base64,${file.base64}`);
          return;
        } catch (e: any) {
          lastErr = e?.message ?? 'chart load failed';
        }
      }
      if (!cancelled) setErr(lastErr);
    })();
    return () => { cancelled = true; };
  }, [agentSessionId, relPath]);

  if (err) return <StatusIndicator type="warning">{relPath}: {err}</StatusIndicator>;
  if (!src) return <Box color="text-status-inactive" fontSize="body-s"><Spinner size="normal" /> {relPath}</Box>;
  return (
    <img
      src={src}
      alt={relPath}
      style={{ maxWidth: '100%', borderRadius: 4, border: '1px solid #e0e0e0', display: 'block' }}
    />
  );
};

const StepCard: React.FC<{ step: CodeStep; agentSessionId: string | null }> = ({
  step,
  agentSessionId,
}) => {
  const { t } = useI18n();
  const running = step.status === 'running';
  const failed = step.status === 'failed';
  return (
    <div
      style={{
        border: '1px solid #e0e0e0',
        borderRadius: 8,
        padding: 12,
        background: 'var(--color-background-container-content, #fff)',
      }}
    >
      <SpaceBetween size="xs">
        <SpaceBetween direction="horizontal" size="xs">
          <Box variant="awsui-key-label">
            {t('codePanel.step')} {step.index + 1}
          </Box>
          <Box fontWeight="bold">{step.title}</Box>
          {running && <Badge color="blue">{t('codePanel.running')}</Badge>}
          {failed && <Badge color="red">{t('codePanel.failed')}</Badge>}
          {!running && !failed && <Badge color="green">{t('codePanel.done')}</Badge>}
        </SpaceBetween>

        {/* Code — fenced python block; react-markdown styles it. */}
        <div className="message-markdown">
          <ReactMarkdown>{'```python\n' + step.code + '\n```'}</ReactMarkdown>
        </div>

        {/* stdout — grows as the poll picks up streamed deltas. */}
        {step.stdout && (
          <pre style={preStyle}>{step.stdout}</pre>
        )}
        {step.stderr && (
          <pre style={{ ...preStyle, color: '#d13212' }}>{step.stderr}</pre>
        )}
        {running && !step.stdout && (
          <Box color="text-status-info" fontSize="body-s">
            <Spinner size="normal" /> {t('codePanel.executing')}
          </Box>
        )}

        {/* Charts — inline images from the session workspace. */}
        {step.charts && step.charts.length > 0 && (
          <SpaceBetween size="xs">
            {step.charts.map((c) => (
              <ChartImage key={c} agentSessionId={agentSessionId} relPath={c} />
            ))}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </div>
  );
};

const preStyle: React.CSSProperties = {
  margin: 0,
  padding: '8px 10px',
  background: '#1b1b1b',
  color: '#e6e6e6',
  borderRadius: 4,
  fontSize: 12,
  lineHeight: 1.5,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  maxHeight: '30vh',
  overflowY: 'auto',
};

const CodeInterpreterTab: React.FC<{
  session: CodeSessionInfo | null;
  agentSessionId: string | null;
}> = ({ session, agentSessionId }) => {
  const { t } = useI18n();

  if (!session || !session.steps || session.steps.length === 0) {
    return (
      <Box textAlign="center" color="text-status-inactive" padding={{ vertical: 'xl' }}>
        {t('codePanel.noActive')}
      </Box>
    );
  }

  // Charts must be read from the workspace of the session the run actually
  // executed under — that path is recorded on the row as agentSessionId. The
  // `agentSessionId` PROP is the CURRENT login's session id, which only
  // matches the row for a run started in this very login; after any reload (or
  // when viewing a row from an earlier login) it points at an empty/nonexistent
  // workspace dir and every chart fetch fails with "not a regular file". Prefer
  // the row's own id, falling back to the prop for older rows that lack it.
  const chartSessionId = session.agentSessionId || agentSessionId;

  return (
    <SpaceBetween size="s">
      <Box variant="p">
        <strong>{t('codePanel.task')}:</strong> {session.title}
      </Box>
      {session.lastError && (
        <StatusIndicator type="error">{session.lastError}</StatusIndicator>
      )}
      {session.steps.map((s) => (
        <StepCard key={s.index} step={s} agentSessionId={chartSessionId} />
      ))}
    </SpaceBetween>
  );
};

export default CodeInterpreterTab;
