import { getIdToken } from '../auth/CognitoAuth';
import { getConfig } from '../config';

export interface BrowserSessionInfo {
  sessionId: string;
  agentSessionId: string;
  liveViewUrl: string;
  // "running" — the agent's browse_web tool is actively driving the browser.
  // "idle"    — tool finished cleanly, AgentCore session still alive until
  //             its sessionTimeoutSeconds (the user can keep driving via
  //             Take Control). Live view remains interactive.
  // "failed"  — tool errored out and the session was stopped.
  status: 'running' | 'idle' | 'failed';
  goal: string;
  startedAt: string;
  endedAt?: string;
  lastError?: string;
  // AgentCore browser identifier (e.g. "aws.browser.v1"). Required by
  // UpdateBrowserStream when the user takes/releases control of the live
  // browser. Absent on older rows; frontend falls back to "aws.browser.v1".
  browserIdentifier?: string;
}

export async function fetchActiveBrowserSession(userId: string): Promise<BrowserSessionInfo | null> {
  const base = getConfig().adminApiUrl;
  if (!base) return null;
  const token = await getIdToken();
  const url = `${base.replace(/\/$/, '')}/sessions?action=browser-active&userId=${encodeURIComponent(userId)}`;
  const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) throw new Error(`browser-sessions/active ${res.status}`);
  const body = await res.json();
  if (!body || !body.sessionId) return null;
  return body as BrowserSessionInfo;
}
