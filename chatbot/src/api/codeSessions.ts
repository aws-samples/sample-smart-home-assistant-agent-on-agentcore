import { getIdToken } from '../auth/CognitoAuth';
import { getConfig } from '../config';

// One executed code block within a run. The agent's execute_python tool
// appends these to the smarthome-code-sessions row as it streams output.
export interface CodeStep {
  index: number;
  title: string;
  code: string;
  stdout: string;
  stderr: string;
  exitCode: number | null;
  executionTime: string;
  // Paths relative to the agent session workspace dir, e.g.
  // "code/step-001-0.png". Loaded via fetchWorkspaceFile (InvokeAgentRuntime).
  charts: string[];
  // "running" while the block is executing, "done" on success, "failed" on
  // a non-zero exit or exception.
  status: 'running' | 'done' | 'failed';
}

export interface CodeSessionInfo {
  sessionId: string;
  agentSessionId: string;
  // "running" — at least one block is still executing.
  // "idle"    — the run finished cleanly.
  // "failed"  — a block errored out.
  status: 'running' | 'idle' | 'failed';
  title: string;
  startedAt: string;
  endedAt?: string;
  lastError?: string;
  steps: CodeStep[];
}

export async function fetchActiveCodeSession(userId: string): Promise<CodeSessionInfo | null> {
  const base = getConfig().adminApiUrl;
  if (!base) return null;
  const token = await getIdToken();
  const url = `${base.replace(/\/$/, '')}/sessions?action=code-active&userId=${encodeURIComponent(userId)}`;
  const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) throw new Error(`code-sessions/active ${res.status}`);
  const body = await res.json();
  if (!body || !body.sessionId) return null;
  return body as CodeSessionInfo;
}
