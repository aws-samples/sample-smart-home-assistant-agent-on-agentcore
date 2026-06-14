import { getIdToken } from '../auth/CognitoAuth';
import { getConfig } from '../config';

export type EntryEnvironmentMode = 'default' | 'ab-bundles' | 'ab-targets';

interface CacheEntry {
  mode: EntryEnvironmentMode;
  fetchedAt: number;
}

const TTL_MS = 60_000;
const cache = new Map<string, CacheEntry>();

function adminApiBase(): string {
  const url = getConfig().adminApiUrl;
  return url.endsWith('/') ? url.slice(0, -1) : url;
}

async function fetchMode(email: string): Promise<EntryEnvironmentMode> {
  const token = await getIdToken();
  const res = await fetch(
    `${adminApiBase()}/skills?tenantEnv=1&userId=${encodeURIComponent(email)}`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!res.ok) {
    throw new Error(`tenantEnv fetch failed: ${res.status}`);
  }
  const data = await res.json();
  return (data.mode || 'default') as EntryEnvironmentMode;
}

export async function getTenantMode(email: string): Promise<EntryEnvironmentMode> {
  const now = Date.now();
  const cached = cache.get(email);
  if (cached && now - cached.fetchedAt < TTL_MS) {
    return cached.mode;
  }
  try {
    const mode = await fetchMode(email);
    cache.set(email, { mode, fetchedAt: now });
    return mode;
  } catch (e) {
    console.warn('tenantEnv fetch failed, falling back to default:', e);
    return 'default';
  }
}

export function clearTenantModeCache(email?: string) {
  if (email) cache.delete(email);
  else cache.clear();
}
