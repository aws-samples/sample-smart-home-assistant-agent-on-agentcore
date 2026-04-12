import { getIdToken } from '../auth/CognitoAuth';
import { getConfig } from '../config';

export interface SkillItem {
  userId: string;
  skillName: string;
  description: string;
  instructions: string;
  allowedTools: string[];
  license?: string;
  compatibility?: string;
  metadata?: Record<string, string>;
  createdAt?: string;
  updatedAt?: string;
}

export interface SkillInput {
  userId: string;
  skillName: string;
  description: string;
  instructions: string;
  allowedTools: string[];
  license?: string;
  compatibility?: string;
  metadata?: Record<string, string>;
}

export interface SkillFile {
  path: string;
  size: number;
  lastModified: string;
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
  updates: Partial<Pick<SkillInput, 'description' | 'instructions' | 'allowedTools' | 'license' | 'compatibility' | 'metadata'>>
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

// ---------------------------------------------------------------------------
// Cognito Users & Tool Permissions
// ---------------------------------------------------------------------------

export interface CognitoUserInfo {
  username: string;
  email: string;
  sub: string;
  status: string;
  createdAt: string;
  groups: string[];
}

export interface GatewayTool {
  name: string;
  description: string;
  targetName: string;
}

export interface UserPermissions {
  userId: string;
  allowedTools: string[];
  updatedAt?: string;
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

// ---------------------------------------------------------------------------
// Skill File Management
// ---------------------------------------------------------------------------

export async function listSkillFiles(
  userId: string,
  skillName: string
): Promise<SkillFile[]> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(userId)}/${encodeURIComponent(skillName)}/files`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to list files (${res.status})`);
  }
  const data = await res.json();
  return data.files || [];
}

export async function getUploadUrl(
  userId: string,
  skillName: string,
  directory: string,
  filename: string,
  contentType: string = 'application/octet-stream'
): Promise<{ uploadUrl: string; key: string }> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(userId)}/${encodeURIComponent(skillName)}/files/upload-url`,
    {
      method: 'POST',
      headers,
      body: JSON.stringify({ directory, filename, contentType }),
    }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to get upload URL (${res.status})`);
  }
  return res.json();
}

export async function uploadSkillFile(uploadUrl: string, file: File): Promise<void> {
  const res = await fetch(uploadUrl, {
    method: 'PUT',
    headers: { 'Content-Type': file.type || 'application/octet-stream' },
    body: file,
  });
  if (!res.ok) {
    throw new Error(`Upload failed (${res.status})`);
  }
}

export async function getDownloadUrl(
  userId: string,
  skillName: string,
  filePath: string
): Promise<string> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(userId)}/${encodeURIComponent(skillName)}/files/download-url`,
    {
      method: 'POST',
      headers,
      body: JSON.stringify({ path: filePath }),
    }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to get download URL (${res.status})`);
  }
  const data = await res.json();
  return data.downloadUrl;
}

export async function deleteSkillFile(
  userId: string,
  skillName: string,
  filePath: string
): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(userId)}/${encodeURIComponent(skillName)}/files?path=${encodeURIComponent(filePath)}`,
    { method: 'DELETE', headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to delete file (${res.status})`);
  }
}

// ---------------------------------------------------------------------------
// Cognito Users & Tool Permissions
// ---------------------------------------------------------------------------

export async function listCognitoUsers(): Promise<CognitoUserInfo[]> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/users`, { headers });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to list users (${res.status})`);
  }
  const data = await res.json();
  return data.users || [];
}

export async function listGatewayTools(): Promise<GatewayTool[]> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/tools`, { headers });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to list tools (${res.status})`);
  }
  const data = await res.json();
  return data.tools || [];
}

export async function getUserPermissions(userId: string): Promise<UserPermissions> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/users/${encodeURIComponent(userId)}/permissions`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to get permissions (${res.status})`);
  }
  return res.json();
}

export async function updateUserPermissions(
  userId: string,
  allowedTools: string[]
): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/users/${encodeURIComponent(userId)}/permissions`,
    {
      method: 'PUT',
      headers,
      body: JSON.stringify({ allowedTools }),
    }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to update permissions (${res.status})`);
  }
}

// ---------------------------------------------------------------------------
// Memories
// ---------------------------------------------------------------------------

export interface MemoryRecord {
  id: string;
  type: string;
  text: string;
  strategy: string;
  createdAt: string;
}

export async function listMemoryActors(): Promise<string[]> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/memories`, { headers });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to list memory actors (${res.status})`);
  }
  const data = await res.json();
  return data.actors || [];
}

export async function getMemoryRecords(actorId: string): Promise<MemoryRecord[]> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/memories/${encodeURIComponent(actorId)}`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to get memory records (${res.status})`);
  }
  const data = await res.json();
  return data.records || [];
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

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
