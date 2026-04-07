import { getIdToken } from '../auth/CognitoAuth';
import { getConfig } from '../config';

export interface SkillItem {
  userId: string;
  skillName: string;
  description: string;
  instructions: string;
  allowedTools: string[];
  createdAt?: string;
  updatedAt?: string;
}

export interface SkillInput {
  userId: string;
  skillName: string;
  description: string;
  instructions: string;
  allowedTools: string[];
}

function getBaseUrl(): string {
  const url = getConfig().adminApiUrl;
  return url.endsWith('/') ? url.slice(0, -1) : url;
}

async function authHeaders(): Promise<Record<string, string>> {
  const token = await getIdToken();
  return {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
  };
}

export async function listSkills(userId: string = '__global__'): Promise<SkillItem[]> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills?userId=${encodeURIComponent(userId)}`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to list skills (${res.status})`);
  }
  const data = await res.json();
  return data.skills || [];
}

export async function getSkill(userId: string, skillName: string): Promise<SkillItem> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(userId)}/${encodeURIComponent(skillName)}`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to get skill (${res.status})`);
  }
  return res.json();
}

export async function createSkill(skill: SkillInput): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/skills`, {
    method: 'POST',
    headers,
    body: JSON.stringify(skill),
  });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to create skill (${res.status})`);
  }
}

export async function updateSkill(
  userId: string,
  skillName: string,
  updates: Partial<Pick<SkillInput, 'description' | 'instructions' | 'allowedTools'>>
): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(userId)}/${encodeURIComponent(skillName)}`,
    {
      method: 'PUT',
      headers,
      body: JSON.stringify(updates),
    }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to update skill (${res.status})`);
  }
}

export async function deleteSkill(userId: string, skillName: string): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(userId)}/${encodeURIComponent(skillName)}`,
    { method: 'DELETE', headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to delete skill (${res.status})`);
  }
}

export async function listUsers(): Promise<string[]> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/skills/users`, { headers });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to list users (${res.status})`);
  }
  const data = await res.json();
  return data.userIds || [];
}

export interface UserSettings {
  userId: string;
  modelId: string;
}

export async function getSettings(userId: string): Promise<UserSettings> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/settings/${encodeURIComponent(userId)}`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to get settings (${res.status})`);
  }
  return res.json();
}

export async function updateSettings(
  userId: string,
  settings: { modelId: string }
): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/settings/${encodeURIComponent(userId)}`,
    {
      method: 'PUT',
      headers,
      body: JSON.stringify(settings),
    }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to update settings (${res.status})`);
  }
}

export interface SessionInfo {
  userId: string;
  sessionId: string;
  lastActiveAt: string;
}

export async function listSessions(): Promise<SessionInfo[]> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/sessions`, { headers });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to list sessions (${res.status})`);
  }
  const data = await res.json();
  return data.sessions || [];
}

export async function stopSession(sessionId: string): Promise<void> {
  // Call AgentCore Runtime StopRuntimeSession API directly with JWT
  // (Lambda can't do this because the runtime uses JWT auth, not SigV4)
  const token = await getIdToken();
  const config = getConfig();
  const encodedArn = encodeURIComponent(config.agentRuntimeArn);
  const url = `https://bedrock-agentcore.${config.region}.amazonaws.com/runtimes/${encodedArn}/stopruntimesession`;

  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json',
      'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': sessionId,
    },
    body: JSON.stringify({}),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Failed to stop session (${res.status}): ${body}`);
  }
}
