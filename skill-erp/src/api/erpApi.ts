import { getIdToken } from '../auth/CognitoAuth';
import { getConfig } from '../config';

export interface MetadataEntry {
  key: string;
  value: string;
}

export interface MyRecord {
  recordId: string;
  name: string;
  description: string;
  status: string;
  createdAt: string;
  updatedAt: string;
  instructions?: string;
  allowedTools?: string[];
  license?: string;
  compatibility?: string;
  metadata?: Record<string, string>;
}

export interface CreateRecordInput {
  skillName: string;
  description: string;
  instructions: string;
  allowedTools: string[];
  license?: string;
  compatibility?: string;
  metadata?: Record<string, string>;
}

export interface UpdateRecordInput {
  description?: string;
  instructions?: string;
  allowedTools?: string[];
  license?: string;
  compatibility?: string;
  metadata?: Record<string, string>;
}

function getBaseUrl(): string {
  const url = getConfig().erpApiUrl;
  return url.endsWith('/') ? url.slice(0, -1) : url;
}

async function authHeaders(): Promise<Record<string, string>> {
  const token = await getIdToken();
  return {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
  };
}

export async function listMyRecords(): Promise<MyRecord[]> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/my-skills`, { headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to list records (${res.status})`);
  }
  const data = await res.json();
  return data.records || [];
}

export async function getMyRecord(recordId: string): Promise<MyRecord> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/my-skills/${encodeURIComponent(recordId)}`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to get record (${res.status})`);
  }
  return res.json();
}

export async function createMyRecord(input: CreateRecordInput): Promise<{ recordId: string }> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/my-skills`, {
    method: 'POST',
    headers,
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to create record (${res.status})`);
  }
  return res.json();
}

export async function updateMyRecord(
  recordId: string,
  input: UpdateRecordInput
): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/my-skills/${encodeURIComponent(recordId)}`,
    {
      method: 'PUT',
      headers,
      body: JSON.stringify(input),
    }
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to update record (${res.status})`);
  }
}

export async function deleteMyRecord(recordId: string): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/my-skills/${encodeURIComponent(recordId)}`,
    { method: 'DELETE', headers }
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to delete record (${res.status})`);
  }
}

// ---------------------------------------------------------------------------
// A2A Agents
// ---------------------------------------------------------------------------

export interface A2ASkill {
  id: string;
  name: string;
  description: string;
  examples: string[];
}

export interface A2ACard {
  name: string;
  description: string;
  endpoint: string;
  version: string;
  provider: string;
  capabilities: {
    streaming: boolean;
    pushNotifications: boolean;
    stateTransitionHistory: boolean;
  };
  auth: 'none' | 'bearer' | 'apiKey';
  tags: string[];
  skills: A2ASkill[];
}

export interface MyA2aRecord {
  recordId: string;
  name: string;
  description: string;
  status: string;
  createdAt: string;
  updatedAt: string;
  card: A2ACard;
}

export async function listMyA2aAgents(): Promise<MyA2aRecord[]> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/my-a2a-agents`, { headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to list A2A records (${res.status})`);
  }
  const data = await res.json();
  return data.records || [];
}

export async function createMyA2aAgent(card: A2ACard): Promise<{ recordId: string }> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/my-a2a-agents`, {
    method: 'POST',
    headers,
    body: JSON.stringify(card),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to create A2A record (${res.status})`);
  }
  return res.json();
}

export async function updateMyA2aAgent(recordId: string, card: A2ACard): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/my-a2a-agents/${encodeURIComponent(recordId)}`,
    { method: 'PUT', headers, body: JSON.stringify(card) }
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to update A2A record (${res.status})`);
  }
}

export async function deleteMyA2aAgent(recordId: string): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/my-a2a-agents/${encodeURIComponent(recordId)}`,
    { method: 'DELETE', headers }
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to delete A2A record (${res.status})`);
  }
}
