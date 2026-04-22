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
