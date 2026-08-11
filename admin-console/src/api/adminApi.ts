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
  /** Optional per-user Bedrock multimodal model used by the vision captioning
   *  pipeline. Empty string = use the global default. */
  visionModelId?: string;
  /** IANA name, e.g. `Asia/Shanghai`. Empty means UTC, which is what every
   *  time-triggered scene used before this field existed. Handed to EventBridge
   *  Scheduler as `ScheduleExpressionTimezone`, so DST is the service's problem
   *  rather than ours. */
  timezone?: string;
  /** Decimal degrees, or null when unset. Required together — the sunrise /
   *  sunset trigger cannot be computed from one of them. */
  latitude?: number | null;
  longitude?: number | null;
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
  // Only the fields present are written; the rest keep their stored value. Pass
  // null for a coordinate to clear it.
  settings: {
    modelId?: string;
    visionModelId?: string;
    timezone?: string;
    latitude?: number | null;
    longitude?: number | null;
  }
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
  totalTokens7d?: number;
  /**
   * The 7-day total split by agent, keyed by the same `agentId` the fleet page
   * uses. Usually one entry: a sub-agent's runtime stamps its own session id
   * rather than inheriting the orchestrator's, so a delegated turn's tokens land
   * under a different session than the turn that caused them.
   */
  tokensByAgent?: Record<string, number>;
  // Which runtime the session lives on — the admin Lambda derives this from
  // the DynamoDB sort key (`__session_text__` vs `__session_voice__`) so the
  // stop-session call targets the correct runtime ARN.
  kind?: 'text' | 'voice';
}

// ---------------------------------------------------------------------------
// Agent System Prompts (text / voice)
// ---------------------------------------------------------------------------

/**
 * A prompt-governed agent.
 *
 * `text` and `voice` are the two runtimes whose prompts ship in the agent image.
 * Any other value is an A2A sub-agent's AgentCard name (`light-effect-agent`) —
 * a plain string rather than a union, because the roster is derived from the
 * Registry at runtime and a closed union here would mean editing the frontend
 * every time a specialist is deployed.
 */
export type AgentType = 'text' | 'voice' | (string & {});

export interface PromptRecord {
  // Saved row at the requested scope — "" when the scope has no override.
  body: string;
  updatedAt: string;
  updatedBy: string;
  // true iff `body` came from a persisted DynamoDB row (vs. empty default).
  isOverride: boolean;
  // Current Global-scope body. At Global scope equals `body`; at per-user
  // scope the UI displays it read-only as additive context.
  globalBody: string;
  // Hardcoded default shipped with the agent image. The Global editor's
  // "Revert to Default" button resets the textarea to this.
  builtinDefault: string;
}

export interface AgentPromptsResponse {
  userId: string;
  text: PromptRecord;
  voice: PromptRecord;
}

// Prompts are stored in the same DynamoDB skills table under reserved sort
// keys (`__prompt_text__` / `__prompt_voice__`, or `__prompt_<cardName>__` for a
// sub-agent) and served through the existing /skills endpoints to avoid adding
// Lambda resource-policy entries (admin Lambda's policy is already at the 20 KB
// cap).

const promptSk = (agentType: AgentType) => `__prompt_${agentType}__`;

/**
 * One prompt record. The bundle endpoint returns text+voice only, so a
 * sub-agent's prompt is fetched individually — bundling all eight would make the
 * Prompt tab pay for six reads it does not render.
 */
export async function getAgentPrompt(
  userId: string,
  agentType: AgentType
): Promise<PromptRecord & { userId: string; agentType: string }> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(userId)}/${promptSk(agentType)}`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to load prompt (${res.status})`);
  }
  return res.json();
}

export async function getAgentPrompts(userId: string): Promise<AgentPromptsResponse> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills?userId=${encodeURIComponent(userId)}&promptBundle=1`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to load prompts (${res.status})`);
  }
  return res.json();
}

export async function saveAgentPrompt(
  userId: string,
  agentType: AgentType,
  promptBody: string
): Promise<void> {
  const headers = await authHeaders();
  // Use PUT on the specific path so the Lambda receives both userId and
  // skillName in pathParameters — simpler routing than POST /skills.
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(userId)}/${promptSk(agentType)}`,
    {
      method: 'PUT',
      headers,
      body: JSON.stringify({ promptBody }),
    }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to save prompt (${res.status})`);
  }
}

export async function deleteAgentPrompt(
  userId: string,
  agentType: AgentType
): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(userId)}/${promptSk(agentType)}`,
    { method: 'DELETE', headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to delete prompt override (${res.status})`);
  }
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
  /** Tagged by the admin API so the UI can group and default-check built-ins. */
  source?: 'builtin' | 'gateway';
  /**
   * Agent ids that call this tool, derived from each agent's declared tool list.
   * Shown next to the checkbox so revoking a tool says who it breaks — the flat
   * list gave no hint that `control_device` also carries the scheduled scenes and
   * two specialists.
   */
  consumers?: string[];
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

export interface CreatedCognitoUser {
  username: string;
  email: string;
  status: string;
  /** Permanent password generated server-side. Show to admin once; they
   *  hand it to the user out-of-band. */
  password: string;
}

export async function createCognitoUser(email: string): Promise<CreatedCognitoUser> {
  const headers = { ...(await authHeaders()), 'Content-Type': 'application/json' };
  const res = await fetch(`${getBaseUrl()}/users`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ action: 'create', email }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Failed to create user (${res.status})`);
  }
  return res.json();
}

export async function addUserToAdminGroup(username: string): Promise<void> {
  const headers = { ...(await authHeaders()), 'Content-Type': 'application/json' };
  const res = await fetch(`${getBaseUrl()}/users`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ action: 'add-to-admin', username }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Failed to add user to admin (${res.status})`);
  }
}

export async function removeUserFromAdminGroup(username: string): Promise<void> {
  const headers = { ...(await authHeaders()), 'Content-Type': 'application/json' };
  const res = await fetch(`${getBaseUrl()}/users`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ action: 'remove-from-admin', username }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Failed to remove admin (${res.status})`);
  }
}

export async function deleteCognitoUser(username: string): Promise<void> {
  const headers = { ...(await authHeaders()), 'Content-Type': 'application/json' };
  const res = await fetch(`${getBaseUrl()}/users`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ action: 'delete', username }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Failed to delete user (${res.status})`);
  }
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
// Knowledge Base
// ---------------------------------------------------------------------------

export interface KBScopeInfo {
  scope: string;
  documentCount: number;
}

export interface KBStatus {
  initialized: boolean;
  knowledgeBaseId: string;
  dataSourceId: string;
  status: string;
  scopes: KBScopeInfo[];
}

export interface KBDocument {
  name: string;
  key: string;
  size: number;
  lastModified: string;
}

export interface KBSyncJob {
  ingestionJobId: string;
  status: string;
  startedAt: string;
  updatedAt: string;
  statistics: Record<string, any>;
}

export async function getKBStatus(): Promise<KBStatus> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/knowledge-bases?action=status`, { headers });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to get KB status (${res.status})`);
  }
  return res.json();
}

export async function listKBDocuments(scope: string): Promise<KBDocument[]> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/knowledge-bases?action=documents&scope=${encodeURIComponent(scope)}`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to list KB documents (${res.status})`);
  }
  const data = await res.json();
  return data.documents || [];
}

export async function getKBUploadUrl(
  scope: string,
  filename: string,
  contentType: string = 'application/octet-stream'
): Promise<{ uploadUrl: string; key: string }> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/knowledge-bases`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ action: 'upload-url', scope, filename, contentType }),
  });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to get upload URL (${res.status})`);
  }
  return res.json();
}

export async function uploadKBDocument(uploadUrl: string, file: File): Promise<void> {
  const res = await fetch(uploadUrl, {
    method: 'PUT',
    headers: { 'Content-Type': file.type || 'application/octet-stream' },
    body: file,
  });
  if (!res.ok) {
    throw new Error(`Upload failed (${res.status})`);
  }
}

export async function deleteKBDocument(key: string): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/knowledge-bases`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ action: 'delete', key }),
  });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to delete document (${res.status})`);
  }
}

export async function startKBSync(): Promise<{ ingestionJobId: string; status: string }> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/knowledge-bases`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ action: 'sync' }),
  });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to start sync (${res.status})`);
  }
  return res.json();
}

export async function getKBSyncStatus(): Promise<{ status: string; jobs: KBSyncJob[] }> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/knowledge-bases?action=sync-status`, { headers });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to get sync status (${res.status})`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// AgentCore Registry
// ---------------------------------------------------------------------------

export interface RegistryRecord {
  recordId: string;
  name: string;
  description: string;
  status: string;
  recordVersion: string;
  createdAt: string;
  updatedAt: string;
}

export async function listRegistryRecords(status: string = 'APPROVED'): Promise<RegistryRecord[]> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/registry/records?status=${encodeURIComponent(status)}`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to list registry records (${res.status})`);
  }
  const data = await res.json();
  return data.records || [];
}

/**
 * Approve, reject or deprecate a published skill.
 *
 * The approval state machine is Registry-managed; until now nothing in this
 * product called it, so a skill published from the Skill ERP sat in
 * PENDING_APPROVAL and only the AWS console could move it.
 *
 * `reason` is stored as the record's `statusReason`, which is the only place the
 * Registry keeps *why* — and therefore the only feedback the skill's author gets.
 * The backend requires it for a rejection.
 */
export async function reviewRegistryRecord(
  recordId: string,
  decision: 'approve' | 'reject' | 'deprecate',
  reason = ''
): Promise<{ status: string; previousStatus: string; reviewedBy: string }> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/registry/records`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ recordId, decision, reason }),
  });
  const body = await res.json().catch(() => ({} as any));
  if (!res.ok) {
    throw new Error(body.error || `Failed to review record (${res.status})`);
  }
  return body;
}

export interface ImportRegistryRecordsResult {
  imported: Array<{ recordId: string; skillName: string; userId: string }>;
  errors: string[];
}

export async function importRegistryRecords(
  recordIds: string[],
  userId: string
): Promise<ImportRegistryRecordsResult> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/registry/import`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ recordIds, userId }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to import records (${res.status})`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

export async function stopSession(sessionId: string, kind?: 'text' | 'voice'): Promise<void> {
  // The runtime uses AWS_IAM (SigV4) auth now (see docs §9.7), so calling
  // StopRuntimeSession directly from the browser with a Bearer JWT fails with
  // 403 "Authorization method mismatch". Route through the admin Lambda
  // instead — it already has bedrock-agentcore:StopRuntimeSession granted.
  //
  // `kind` selects which runtime ARN to target. Text and voice sessions share
  // the same sessionId (chatbot derives it from the Cognito sub) but live on
  // separate runtimes, so the caller must say which one to stop. Defaults to
  // text for backwards-compat if omitted.
  const headers = await authHeaders();
  const qs = kind ? `?kind=${encodeURIComponent(kind)}` : '';
  const res = await fetch(
    `${getBaseUrl()}/sessions/${encodeURIComponent(sessionId)}/stop${qs}`,
    { method: 'POST', headers },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to stop session (${res.status})`);
  }
}

// ---------------------------------------------------------------------------
// A2A Agents (Integration Registry)
// ---------------------------------------------------------------------------

export interface A2AAgentCard {
  name: string;
  description: string;
  url: string;
  version: string;
  provider?: { organization?: string };
  capabilities: {
    streaming?: boolean;
    pushNotifications?: boolean;
    stateTransitionHistory?: boolean;
  };
  authentication: { schemes: string[] };
  skills: Array<{ id: string; name: string; description: string; examples: string[] }>;
  tags: string[];
}

export interface A2AAgentRecord {
  recordId: string;
  name: string;
  description: string;
  status: string;
  createdAt: string;
  updatedAt: string;
  card: A2AAgentCard;
  publishedBy: string;
}

/** One entry in the agent fleet. */
export interface FleetAgent {
  agentId: string;
  displayName: string;
  displayNameZh?: string;
  runtimeName: string;
  runtimeArn: string;
  runtimeId: string;
  /** orchestrator | specialist | voice | tool */
  kind: string;
  description: string;
  version?: string;
  skills: { id: string; name: string; description: string }[];
  recordId: string;
  registryStatus?: string;
  invocationUrl?: string;
  orchestrator?: string;
  targetName?: string;
  /** False for an approved Registry record with no live runtime behind it. */
  live: boolean;
  /** 24h CloudWatch totals; null when the metric had no datapoints. */
  invocations: number | null;
  errors: number | null;
  throttles?: number | null;
  latencyP95Ms: number | null;
}

/** The whole fleet: runtimes joined with their Registry records and metadata. */
export async function listAgentFleet(): Promise<FleetAgent[]> {
  const headers = await authHeaders();
  // Same consolidated resource as the A2A actions — a new API Gateway path would
  // push the admin Lambda's resource policy past its 20KB cap.
  const res = await fetch(`${getBaseUrl()}/registry/records?action=fleet`, {
    headers,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to list the agent fleet (${res.status})`);
  }
  const data = await res.json();
  return data.agents || [];
}

// ---------------------------------------------------------------------------
// Scenarios (saved automations, across users)
// ---------------------------------------------------------------------------

/** One saved automation, as the operator view needs it. */
export interface ScenarioRow {
  userId: string;
  scenarioId: string;
  name: string;
  description: string;
  /** Plain-language rendering of the trigger, built by shared/scenarios.py so the
   *  console and the agent describe a trigger identically. */
  triggerDescription: string;
  trigger: { sceneType?: string; subject?: string; conditionValue?: unknown };
  actionCount: number;
  isActive: boolean;
  isTemplate: boolean;
  /** True when this scene owns a schedule of its own (time and solar do). */
  scheduled: boolean;
  cron: string;
  /** The zone the cron is evaluated in. "UTC" for solar, since a sunrise time is
   *  already absolute. Empty for a scene with no schedule. */
  timezone: string;
  /** From the runner. The only way to answer "did it fire?" for something that
   *  runs at 07:30 when nobody is watching. */
  lastRunAt: string;
  lastRunOk?: boolean | null;
  lastRunDetail: string;
  updatedAt: string;
  /** Which agent wrote it. Rows created before the agent was renamed carry the
   *  old name, and that is left as it is rather than rewritten. */
  source: string;
}

export async function listScenarios(): Promise<ScenarioRow[]> {
  const headers = await authHeaders();
  // Same consolidated resource as the fleet and A2A actions — a new API Gateway
  // path would push the admin Lambda's resource policy past its 20KB cap.
  const res = await fetch(`${getBaseUrl()}/registry/records?action=scenarios`, {
    headers,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to list scenarios (${res.status})`);
  }
  const data = await res.json();
  return data.scenarios || [];
}

export interface SyncSchedulesResult {
  synced: boolean;
  created?: string[];
  updated?: string[];
  deleted?: string[];
  failed?: Array<{ name: string; error: string }>;
  reason?: string;
}

/**
 * Reconcile EventBridge Scheduler against the saved scenes.
 *
 * Runs on every scene save too; exposed here because reconciliation is
 * self-healing and an operator who suspects a schedule has drifted should be able
 * to say so. `failed` includes scenes that CANNOT be scheduled (a sunrise trigger
 * whose owner has no coordinates) as well as API errors — from the user's side
 * both mean the same thing: it will not fire.
 */
export async function syncScenarioSchedules(): Promise<SyncSchedulesResult> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/registry/records?action=sync-schedules`,
    { method: 'POST', headers, body: JSON.stringify({}) }
  );
  const body = await res.json().catch(() => ({} as any));
  if (!res.ok) {
    throw new Error(body.error || `Failed to reconcile schedules (${res.status})`);
  }
  return body;
}

/** A scene in its portable form — what export emits and import accepts. */
export interface PortableScene {
  name: string;
  description?: string;
  trigger: Record<string, unknown>;
  deviceActions: Array<Record<string, unknown>>;
  isActive?: boolean;
  isTemplate?: boolean;
}

export interface SceneExport {
  version: number;
  exportedFor: string;
  count: number;
  scenes: PortableScene[];
}

export interface SceneImportResult {
  created: Array<{ scenarioId: string; name: string; trigger: string;
                   warnings?: string[] }>;
  failed: Array<{ index: number; name?: string; error: string }>;
  createdCount: number;
  failedCount: number;
  note?: string;
}

/**
 * One user's scenes as portable JSON (spec 5 S6, "scenes as code").
 *
 * `userId` is required by the API rather than defaulting to everyone: a scene
 * carries device ids and daily routines, so a fleet-wide dump would be a
 * disclosure bug dressed as convenience.
 */
export async function exportScenes(userId: string): Promise<SceneExport> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/registry/records?action=export-scenes&userId=${encodeURIComponent(userId)}`,
    { headers }
  );
  const body = await res.json().catch(() => ({} as any));
  if (!res.ok) throw new Error(body.error || `Export failed (${res.status})`);
  return body;
}

/**
 * Create scenes from a JSON document.
 *
 * Per-scene outcomes, not all-or-nothing: one bad trigger must not cost the user
 * their other five scenes, so the result names which entries failed and why.
 * Imported scenes are stored but NOT scheduled — Reconcile does that, so parsing a
 * document can never start firing automations as a side effect.
 */
export async function importScenes(
  userId: string, scenes: PortableScene[]
): Promise<SceneImportResult> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/registry/records?action=import-scenes`,
    { method: 'POST', headers, body: JSON.stringify({ userId, scenes }) }
  );
  const body = await res.json().catch(() => ({} as any));
  if (!res.ok) throw new Error(body.error || `Import failed (${res.status})`);
  return body;
}

export async function listA2aAgents(): Promise<A2AAgentRecord[]> {
  const headers = await authHeaders();
  // Reuses /registry/records?action=a2a-list — consolidated on a single API
  // Gateway resource to stay under the admin Lambda's 20KB policy cap.
  const res = await fetch(`${getBaseUrl()}/registry/records?action=a2a-list`, {
    headers,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to list A2A agents (${res.status})`);
  }
  const data = await res.json();
  return data.records || [];
}

// ---------------------------------------------------------------------------
// A2A Permissions (per-user grants: recordId → [skillId, ...])
//
// The server reuses /users/{userId}/permissions with ?action=a2a to keep the
// admin Lambda's resource policy under the 20KB cap.
// ---------------------------------------------------------------------------

export interface A2AAvailableAgent {
  recordId: string;
  name: string;
  description: string;
  skills: Array<{ id: string; name: string; description: string }>;
}

export interface UserA2APermissions {
  userId: string;
  a2aGrants: { [recordId: string]: string[] };
  availableAgents: A2AAvailableAgent[];
  /** Grants whose Registry record no longer exists — already filtered out of
   *  `a2aGrants` by the API, because PUT validates every recordId and one dead
   *  entry would make every save fail with a 400. Reported so an admin asking
   *  "why did this user lose access" can see the record was replaced. */
  staleGrants?: string[];
  /** Set when `availableAgents` is empty because the Registry lookup FAILED
   *  rather than because there is nothing to grant. Without it the two cases are
   *  the same empty list, and the console showed "no agents available" for a
   *  wrong registryId, a missing IAM action and a genuinely empty registry
   *  alike. */
  catalogError?: string;
  updatedAt?: string;
}

export interface A2AGrantSummary {
  userId: string;
  skillIds: string[];
  updatedAt: string;
}

export async function getUserA2APermissions(
  userId: string
): Promise<UserA2APermissions> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/users/${encodeURIComponent(userId)}/permissions?action=a2a`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(
      body.error || `Failed to get A2A permissions (${res.status})`
    );
  }
  const data = await res.json();
  return {
    userId: data.userId,
    a2aGrants: data.a2aGrants || {},
    availableAgents: data.availableAgents || [],
    staleGrants: data.staleGrants || [],
    catalogError: data.catalogError || '',
    updatedAt: data.updatedAt,
  };
}

export async function updateUserA2APermissions(
  userId: string,
  a2aGrants: { [recordId: string]: string[] }
): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/users/${encodeURIComponent(userId)}/permissions?action=a2a`,
    {
      method: 'PUT',
      headers,
      body: JSON.stringify({ a2aGrants }),
    }
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    const detail = body.details ? `: ${(body.details as string[]).join('; ')}` : '';
    throw new Error(
      (body.error || `Failed to update A2A permissions (${res.status})`) + detail
    );
  }
}

export async function listA2aGrantsForRecord(
  recordId: string
): Promise<A2AGrantSummary[]> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/registry/records?action=a2a-grants&recordId=${encodeURIComponent(recordId)}`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as any));
    throw new Error(body.error || `Failed to list A2A grants (${res.status})`);
  }
  const data = await res.json();
  return data.grants || [];
}

// ---------------------------------------------------------------------------
// AgentCore Optimization (recommendations, configuration bundles, A/B tests).
// See docs/superpowers/specs/2026-05-14-agentcore-optimization-design.md.
// ---------------------------------------------------------------------------
/**
 * An optimisation target. The three built-ins, plus any deployed agent's id —
 * the backend resolves an unrecognised value against the fleet and analyses THAT
 * agent's traces. A closed union would have to be edited for every new
 * specialist, which is the drift this page exists to avoid.
 */
export type OptAgentType = 'text' | 'voice' | 'tool_desc' | (string & {});
export type OptRecStatus = 'PENDING' | 'IN_PROGRESS' | 'COMPLETED' | 'FAILED' | 'DELETING';
export type OptABExecutionStatus = 'NOT_STARTED' | 'PAUSED' | 'RUNNING' | 'STOPPED';
export type OptABStatus =
  | 'CREATING'
  | 'ACTIVE'
  | 'CREATE_FAILED'
  | 'UPDATING'
  | 'UPDATE_FAILED'
  | 'DELETING'
  | 'DELETE_FAILED'
  | 'FAILED';

export interface OptBundleRef {
  bundleArn: string;
  bundleVersion: string;
}

export interface OptRecommendation {
  recommendationId: string;
  recommendationArn?: string;
  agentType: OptAgentType;
  status: OptRecStatus;
  evaluatorArn: string;
  createdAt: string;
  appliedAt?: string;
  recommendedSystemPrompt?: string;
  tools?: { toolName: string; recommendedToolDescription: string }[];
  errorCode?: string;
  errorMessage?: string;
}

export interface OptBundle {
  bundleArn: string;
  bundleName: string;
  latestVersionId: string;
  agentType: OptAgentType;
  sourceRecommendationId?: string;
  createdAt: string;
}

export interface OptABTestSummary {
  testId: string;
  testArn?: string;
  agentType: OptAgentType;
  status?: OptABStatus;
  executionStatus: OptABExecutionStatus;
  createdAt: string;
  autoStopAt: string;
  winner?: string;
}

export interface OptABTestDetail {
  testId: string;
  status: OptABStatus;
  executionStatus: OptABExecutionStatus;
  perVariant: { variantName: string; meanScore: number | null; sampleCount: number | null }[];
  pValue: number | null;
  significant: boolean | null;
  winner: string | null;
  cloudwatchDashboardUrl: string;
  routingMode?: 'target-based';
  controlEndpoint?: string;
  treatmentEndpoint?: string;
}

export interface StartRecommendationInput {
  scope: string;
  agentType: OptAgentType;
  evaluatorArn: string;
  logGroupArn?: string;
  startTime: string;
  endTime: string;
  ruleFilter?: unknown;
  name?: string;
}

export interface StartABTestInput {
  // Target-based A/B routing (text agent only). Variants reference runtime
  // endpoint qualifiers ("control" / "treatment") via gateway targets, not
  // configuration bundle versions. See spec
  // 2026-05-17-agentcore-optimization-target-based-design.md §4.2.
  agentType: 'text';
  controlEndpoint: string;
  treatmentEndpoint: string;
  variantWeights: { control: number; treatment: number };
  durationDays: 1 | 3 | 7 | 14;
  name?: string;
  scope?: string;  // server enforces __global__; included for explicit error visibility
  roleArn?: string;
}

export interface OptABToggle {
  enabled: boolean;
  updatedAt?: string;
  updatedBy?: string;
}

async function optFetch<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}${path}`, {
    method,
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({} as any));
    const code = errBody.error || `HTTP_${res.status}`;
    const msg = errBody.message || `${method} ${path} failed (${res.status})`;
    throw new Error(`${code}: ${msg}`);
  }
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

export const startRecommendation = (input: StartRecommendationInput) =>
  optFetch<{ recommendationId: string; recommendationArn: string; status: OptRecStatus }>(
    'POST', '/optimization/recommendations', input,
  );

export const listRecommendations = (scope: string) =>
  optFetch<OptRecommendation[]>(
    'GET', `/optimization/recommendations?scope=${encodeURIComponent(scope)}`,
  );

export const getRecommendation = (recId: string) =>
  optFetch<OptRecommendation>('GET', `/optimization/recommendations/${encodeURIComponent(recId)}`);

export const deleteRecommendation = (recId: string) =>
  optFetch<void>('DELETE', `/optimization/recommendations/${encodeURIComponent(recId)}`);

export interface ApplyRecommendationResponse {
  // text/voice apply: just confirms the prompt row was written. Bundle
  // fields are absent for prompt agentTypes (target-based redesign).
  applied?: boolean;
  agentType?: OptAgentType;
  scope?: string;
  // tool_desc apply: still creates a configuration bundle for rollback.
  appliedBundleArn?: string;
  appliedBundleVersionId?: string;
}

export const applyRecommendation = (recId: string) =>
  optFetch<ApplyRecommendationResponse>(
    'POST', `/optimization/recommendations/${encodeURIComponent(recId)}/apply`,
  );

export const getABToggle = () =>
  optFetch<OptABToggle>('GET', '/optimization/ab-toggle');

export const setABToggle = (enabled: boolean) =>
  optFetch<{ enabled: boolean; stoppedTestId?: string }>(
    'PUT', '/optimization/ab-toggle', { enabled },
  );

export const listBundles = (scope: string, agentType?: OptAgentType) => {
  const qs = new URLSearchParams({ scope });
  if (agentType) qs.set('agentType', agentType);
  return optFetch<OptBundle[]>('GET', `/optimization/bundles?${qs.toString()}`);
};

export const getBundleVersions = (bundleArn: string) =>
  optFetch<{ versions: { versionId: string; createdAt?: string; parentVersionId?: string; branch?: string }[] }>(
    'GET', `/optimization/bundles/${encodeURIComponent(bundleArn)}`,
  );

export const deleteBundle = (bundleArn: string) =>
  optFetch<void>('DELETE', `/optimization/bundles/${encodeURIComponent(bundleArn)}`);

export const listABTests = () =>
  optFetch<OptABTestSummary[]>('GET', '/optimization/ab-tests');

export const startABTest = (input: StartABTestInput) =>
  optFetch<{ testId: string; testArn: string; status: OptABStatus; executionStatus: OptABExecutionStatus; autoStopAt: string }>(
    'POST', '/optimization/ab-tests', input,
  );

export const getABTest = (testId: string) =>
  optFetch<OptABTestDetail>('GET', `/optimization/ab-tests/${encodeURIComponent(testId)}`);

export const stopABTest = (testId: string) =>
  optFetch<{ executionStatus: OptABExecutionStatus; winner?: string }>(
    'POST', `/optimization/ab-tests/${encodeURIComponent(testId)}/stop`,
  );

export type EntryEnvironmentMode = 'default' | 'ab-bundles' | 'ab-targets';

export interface TenantEnvOverride {
  email: string;
  mode: EntryEnvironmentMode;
  updatedAt?: string;
  updatedBy?: string;
}

export async function listTenantEnvs(): Promise<TenantEnvOverride[]> {
  const headers = await authHeaders();
  const res = await fetch(`${getBaseUrl()}/skills?tenantEnv=1`, { headers });
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to list tenant environments (${res.status})`);
  }
  const data = await res.json();
  return data.overrides || [];
}

export async function getTenantEnv(email: string): Promise<{ mode: EntryEnvironmentMode; updatedAt?: string; updatedBy?: string }> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills?tenantEnv=1&userId=${encodeURIComponent(email)}`,
    { headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to get tenant environment (${res.status})`);
  }
  return res.json();
}

export class PerUserPromptWillBeMaskedError extends Error {
  constructor(public email: string, message: string) {
    super(message);
    this.name = 'PerUserPromptWillBeMaskedError';
  }
}

export async function putTenantEnv(
  email: string,
  mode: EntryEnvironmentMode,
  acknowledgeMaskedOverride = false,
): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(email)}/__tenant_env__`,
    {
      method: 'PUT',
      headers,
      body: JSON.stringify({ mode, acknowledgeMaskedOverride }),
    }
  );
  if (res.status === 409) {
    const body = await res.json();
    if (body.error === 'PerUserPromptWillBeMasked') {
      throw new PerUserPromptWillBeMaskedError(email, body.message);
    }
  }
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to set tenant environment (${res.status})`);
  }
}

export async function deleteTenantEnv(email: string): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(
    `${getBaseUrl()}/skills/${encodeURIComponent(email)}/__tenant_env__`,
    { method: 'DELETE', headers }
  );
  if (!res.ok) {
    const body = await res.json();
    throw new Error(body.error || `Failed to delete tenant environment (${res.status})`);
  }
}

// ---------------------------------------------------------------------------
// Overview ops dashboard — GET /dashboard
// Two-stage: the fast part is CloudWatch + control-plane reads; the spans part
// runs Logs Insights over aws/spans and takes ~5-20s, so the UI requests it
// separately and fills those cards in when it lands. See dashboard.py.
// ---------------------------------------------------------------------------

// Widened to 90d on 2026-08-11. Measured first: one 90d Logs Insights query runs
// in 3.2s against a 22s budget, so the longer windows cost nothing. See
// dashboard.py's RANGES comment for why the query is NOT chunked.
export type DashboardRange = '24h' | '7d' | '30d' | '60d' | '90d';
export type DashboardDim = 'user' | 'tenant' | 'agent';

export interface MetricSeries {
  timestamps: string[];
  values: number[];
}

/**
 * One agent runtime's slice of the fleet-wide health totals.
 *
 * The dashboard aggregates every registered runtime (text, voice, bundles, and
 * each A2A specialist), so the top-line numbers alone can't say which agent
 * produced them. `latencyP95` is null when that runtime emitted no latency
 * samples in the window.
 */
export interface RuntimeBreakdown {
  name: string;
  invocations?: number;
  sessions?: number;
  userErrors?: number;
  systemErrors?: number;
  throttles?: number;
  latencyP95?: number | null;
}

export interface DashboardHealth {
  available: boolean;
  reason?: string;
  invocations: number;
  sessions: number;
  userErrors: number;
  systemErrors: number;
  throttles: number;
  /** null when there was no traffic — 0/0 would render a misleading 0%. */
  errorRate: number | null;
  qps: number;
  latencyP95Ms: number | null;
  /** ActiveSessionCount only exists account-wide, not per runtime. */
  activeSessionsAccount: number | null;
  series: Record<string, MetricSeries>;
  /** Per-runtime split of the totals above. Absent on older backends. */
  runtimes?: RuntimeBreakdown[];
}

export interface EvaluatorScore {
  name: string;
  average: number;
  latest: number;
  drift: number | null;
  /** false for Numerical scales (e.g. smarthome_SmartHomeQuality) — not a %. */
  isRatio: boolean;
  timestamps: string[];
  values: number[];
}

export interface DashboardEvaluations {
  available: boolean;
  reason?: string;
  evaluators: EvaluatorScore[];
}

export interface DashboardAbComparison {
  available: boolean;
  reason?: string;
  rows?: { variant: string; evaluator: string; average: number; n: number }[];
}

export interface RuntimeEndpointInfo {
  name: string;
  liveVersion: string;
  status: string;
  description: string;
  lastUpdatedAt: string;
}

export interface DashboardRelease {
  available: boolean;
  reason?: string;
  runtimeId: string;
  latestVersion: string | null;
  endpoints: RuntimeEndpointInfo[];
  abTests: { name: string; status: string; executionStatus: string; updatedAt: string }[];
  abRunning: boolean;
  tenantOverrides: number;
  /** Derived, not a native AgentCore field — see dashboard.py _rollout_stage. */
  rolloutStage: string;
  rollbackHistory: {
    eventTime: string; username: string; endpointName: string; targetVersion: string;
  }[];
  historyRetentionDays: number;
}

export interface SpanTrendPoint {
  day: string;
  n: number;
  ttftP95: number;
  ttftP99: number;
  inputTokens: number;
  outputTokens: number;
}

export interface AttributionRow {
  key: string;
  inputTokens: number;
  outputTokens: number;
  sessions: number;
}

export interface DashboardSpans {
  available: boolean;
  reason?: string;
  trend: SpanTrendPoint[];
  attribution: AttributionRow[];
  dim: DashboardDim;
  // Where span history actually begins: the oldest instant any span log group can
  // still answer for. Probed per request because it MOVES — `aws/spans` keeps 30
  // rolling days while the per-runtime groups never expire. Null when it could not
  // be determined, which the UI must report as unknown rather than as "today".
  dataFrom?: string | null;
  totals: {
    inputTokens: number;
    outputTokens: number;
    spans: number;
    ttftP95Ms: number | null;
    ttftP99Ms: number | null;
  };
}

/** Real thumbs up/down, replacing the hardcoded MOCK_SATISFACTION block. */
export interface DashboardSatisfaction {
  available: boolean;
  reason?: string;
  thumbsUp?: number;
  thumbsDown?: number;
  /** 1-5, derived from the up/down split. Null only when nothing has been voted. */
  csat?: number | null;
  csatScale?: number;
  /** Fraction of votes filed by the simulator, so the card can say so. */
  simulatedShare?: number;
  trend?: Array<{ day: string; up: number; down: number; downRate: number }>;
  byAgent?: Array<{ agent: string; up: number; down: number }>;
  recentReasons?: Array<{ ts: string; vote: string; reason: string; source: string }>;
}

export interface DashboardFastResponse {
  range: DashboardRange;
  part: 'fast';
  generatedAt: string;
  cached: boolean;
  cachedAt?: string;
  health: DashboardHealth;
  evaluations: DashboardEvaluations;
  abComparison: DashboardAbComparison;
  satisfaction: DashboardSatisfaction;
  release: DashboardRelease;
}

export interface DashboardSpansResponse {
  range: DashboardRange;
  part: 'spans';
  generatedAt: string;
  cached: boolean;
  cachedAt?: string;
  spans: DashboardSpans;
}

export async function getDashboardFast(
  range: DashboardRange,
  refresh = false,
): Promise<DashboardFastResponse> {
  const headers = await authHeaders();
  const qs = new URLSearchParams({ range });
  if (refresh) qs.set('refresh', '1');
  const res = await fetch(`${getBaseUrl()}/dashboard?${qs}`, { headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Failed to load dashboard (${res.status})`);
  }
  return res.json();
}

export async function getDashboardSpans(
  range: DashboardRange,
  dim: DashboardDim,
  refresh = false,
): Promise<DashboardSpansResponse> {
  const headers = await authHeaders();
  const qs = new URLSearchParams({ range, part: 'spans', dim });
  if (refresh) qs.set('refresh', '1');
  const res = await fetch(`${getBaseUrl()}/dashboard?${qs}`, { headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Failed to load token metrics (${res.status})`);
  }
  return res.json();
}
