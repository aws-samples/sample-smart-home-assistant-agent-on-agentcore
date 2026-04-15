import React, { useState, useEffect, useCallback } from 'react';
import {
  listSkills,
  createSkill,
  updateSkill,
  deleteSkill,
  listUsers,
  getSettings,
  updateSettings,
  listSessions,
  stopSession,
  listSkillFiles,
  getUploadUrl,
  uploadSkillFile,
  getDownloadUrl,
  deleteSkillFile,
  listCognitoUsers,
  listGatewayTools,
  getUserPermissions,
  updateUserPermissions,
  listMemoryActors,
  getMemoryRecords,
  getKBStatus,
  listKBDocuments,
  getKBUploadUrl,
  uploadKBDocument,
  deleteKBDocument,
  startKBSync,
  getKBSyncStatus,
  SkillItem,
  SkillInput,
  SkillFile,
  SessionInfo,
  CognitoUserInfo,
  GatewayTool,
  MemoryRecord,
  KBDocument,
  KBScopeInfo,
  KBSyncJob,
} from '../api/adminApi';
import { getConfig } from '../config';
import { useI18n } from '../i18n';

// Skill name validation (matches Strands SDK pattern)
const SKILL_NAME_RE = /^(?!-)(?!.*--)(?!.*-$)[a-z0-9-]{1,64}$/;

// Available Bedrock model IDs for the model selector
const AVAILABLE_MODELS = [
  { id: 'moonshotai.kimi-k2.5', label: 'Kimi K2.5 (Moonshot)' },
  { id: 'moonshot.kimi-k2-thinking', label: 'Kimi K2 Thinking (Moonshot)' },
  { id: '', label: '── Claude 4.6 ──', disabled: true },
  { id: 'us.anthropic.claude-sonnet-4-6', label: 'Claude Sonnet 4.6' },
  { id: 'us.anthropic.claude-opus-4-6-v1', label: 'Claude Opus 4.6' },
  { id: '', label: '── Claude 4.5 ──', disabled: true },
  { id: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0', label: 'Claude Sonnet 4.5' },
  { id: 'us.anthropic.claude-opus-4-5-20251101-v1:0', label: 'Claude Opus 4.5' },
  { id: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', label: 'Claude Haiku 4.5' },
  { id: '', label: '── Claude 4 ──', disabled: true },
  { id: 'us.anthropic.claude-sonnet-4-20250514-v1:0', label: 'Claude Sonnet 4' },
  { id: 'us.anthropic.claude-opus-4-20250514-v1:0', label: 'Claude Opus 4' },
  { id: 'us.anthropic.claude-opus-4-1-20250805-v1:0', label: 'Claude Opus 4.1' },
  { id: '', label: '── Claude 3.x ──', disabled: true },
  { id: 'us.anthropic.claude-3-7-sonnet-20250219-v1:0', label: 'Claude 3.7 Sonnet' },
  { id: 'us.anthropic.claude-3-5-haiku-20241022-v1:0', label: 'Claude 3.5 Haiku' },
  { id: '', label: '── DeepSeek ──', disabled: true },
  { id: 'deepseek.v3.2', label: 'DeepSeek V3.2' },
  { id: 'deepseek.v3-v1:0', label: 'DeepSeek V3.1' },
  { id: 'deepseek.r1-v1:0', label: 'DeepSeek R1' },
  { id: '', label: '── Qwen ──', disabled: true },
  { id: 'qwen.qwen3-235b-a22b-2507-v1:0', label: 'Qwen3 235B A22B' },
  { id: 'qwen.qwen3-next-80b-a3b', label: 'Qwen3 Next 80B A3B' },
  { id: 'qwen.qwen3-32b-v1:0', label: 'Qwen3 32B (Dense)' },
  { id: 'qwen.qwen3-vl-235b-a22b', label: 'Qwen3 VL 235B A22B' },
  { id: 'qwen.qwen3-coder-480b-a35b-v1:0', label: 'Qwen3 Coder 480B A35B' },
  { id: 'qwen.qwen3-coder-30b-a3b-v1:0', label: 'Qwen3 Coder 30B A3B' },
  { id: '', label: '── GLM (Z.AI) ──', disabled: true },
  { id: 'zai.glm-5', label: 'GLM 5' },
  { id: 'zai.glm-4.7', label: 'GLM 4.7' },
  { id: 'zai.glm-4.7-flash', label: 'GLM 4.7 Flash' },
  { id: '', label: '── MiniMax ──', disabled: true },
  { id: 'minimax.minimax-m2.5', label: 'MiniMax M2.5' },
  { id: 'minimax.minimax-m2.1', label: 'MiniMax M2.1' },
  { id: 'minimax.minimax-m2', label: 'MiniMax M2' },
  { id: '', label: '── Meta Llama ──', disabled: true },
  { id: 'us.meta.llama4-maverick-17b-instruct-v1:0', label: 'Llama 4 Maverick 17B' },
  { id: 'us.meta.llama4-scout-17b-instruct-v1:0', label: 'Llama 4 Scout 17B' },
  { id: 'us.meta.llama3-3-70b-instruct-v1:0', label: 'Llama 3.3 70B Instruct' },
  { id: '', label: '── OpenAI ──', disabled: true },
  { id: 'openai.gpt-oss-120b-1:0', label: 'GPT OSS 120B' },
  { id: 'openai.gpt-oss-20b-1:0', label: 'GPT OSS 20B' },
] as const;

interface MetadataEntry {
  key: string;
  value: string;
}

interface SkillFormData {
  userId: string;
  skillName: string;
  description: string;
  instructions: string;
  allowedTools: string;
  license: string;
  compatibility: string;
  metadata: MetadataEntry[];
}

const emptyForm: SkillFormData = {
  userId: '__global__',
  skillName: '',
  description: '',
  instructions: '',
  allowedTools: 'device_control',
  license: '',
  compatibility: '',
  metadata: [],
};

// ---------------------------------------------------------------------------
// Models Tab — standalone component
// ---------------------------------------------------------------------------
interface ModelsTabProps {
  error: string;
  success: string;
  clearMessages: () => void;
  setError: (msg: string) => void;
  setSuccess: (msg: string) => void;
}

const ModelsTab: React.FC<ModelsTabProps> = ({ error, success, clearMessages, setError, setSuccess }) => {
  const [globalModelId, setGlobalModelId] = useState('');
  const [savedGlobalModelId, setSavedGlobalModelId] = useState('');
  const [users, setUsers] = useState<CognitoUserInfo[]>([]);
  const [userModels, setUserModels] = useState<Record<string, string>>({});
  const [savedUserModels, setSavedUserModels] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const { t } = useI18n();

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const globalSettings = await getSettings('__global__');
      setGlobalModelId(globalSettings.modelId || '');
      setSavedGlobalModelId(globalSettings.modelId || '');

      const cognitoUsers = await listCognitoUsers();
      setUsers(cognitoUsers);

      const models: Record<string, string> = {};
      for (const u of cognitoUsers) {
        try {
          const s = await getSettings(u.email || u.username || u.sub);
          models[u.sub] = s.modelId || '';
        } catch {
          models[u.sub] = '';
        }
      }
      setUserModels(models);
      setSavedUserModels({ ...models });
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [setError]);

  useEffect(() => { loadData(); }, [loadData]);

  const handleSaveGlobal = async () => {
    clearMessages();
    try {
      await updateSettings('__global__', { modelId: globalModelId });
      setSavedGlobalModelId(globalModelId);
      setSuccess(t('models.globalUpdated'));
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleSaveUserModel = async (user: CognitoUserInfo) => {
    clearMessages();
    const userId = user.email || user.username || user.sub;
    const newModel = userModels[user.sub] || '';
    try {
      await updateSettings(userId, { modelId: newModel });
      setSavedUserModels((prev) => ({ ...prev, [user.sub]: newModel }));
      setSuccess(t('models.userUpdated').replace('{user}', user.email || user.username || ''));
    } catch (err: any) {
      setError(err.message);
    }
  };

  return (
    <div className="models-section">
      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      <div className="settings-panel" style={{ marginBottom: '20px' }}>
        <h3 style={{ color: '#fff', margin: '0 0 8px 0', fontSize: '16px' }}>{t('models.globalDefault')}</h3>
        <p className="settings-hint" style={{ margin: '0 0 12px 0' }}>
          {t('models.globalHint')}
        </p>
        <div className="settings-row">
          <select
            className="settings-select"
            value={globalModelId}
            onChange={(e) => setGlobalModelId(e.target.value)}
          >
            <option value="">{t('models.notSet')}</option>
            {AVAILABLE_MODELS.map((m, i) =>
              (m as any).disabled ? (
                <option key={i} disabled>{m.label}</option>
              ) : (
                <option key={m.id} value={m.id}>{m.label}</option>
              )
            )}
          </select>
          <button
            className="btn btn-primary btn-sm"
            onClick={handleSaveGlobal}
            disabled={globalModelId === savedGlobalModelId}
          >
            {t('models.save')}
          </button>
        </div>
      </div>

      <div className="toolbar">
        <div className="toolbar-left">
          <span className="toolbar-label">{t('models.perUser')}</span>
        </div>
        <button className="btn btn-secondary btn-sm" onClick={loadData}>
          {t('models.refresh')}
        </button>
      </div>

      <div className="skills-table-container">
        {loading ? (
          <div className="loading">{t('models.loadingUsers')}</div>
        ) : users.length === 0 ? (
          <div className="empty-state">
            <p>{t('models.noUsers')}</p>
          </div>
        ) : (
          <table className="skills-table">
            <thead>
              <tr>
                <th>{t('models.colEmail')}</th>
                <th>{t('models.colStatus')}</th>
                <th>{t('models.colModel')}</th>
                <th>{t('models.colActions')}</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.sub}>
                  <td className="cell-name">{u.email || u.username}</td>
                  <td>
                    <span className={`badge ${u.status === 'CONFIRMED' ? 'badge-active' : 'badge-inactive'}`}>
                      {u.status}
                    </span>
                  </td>
                  <td>
                    <select
                      className="settings-select"
                      style={{ minWidth: '240px' }}
                      value={userModels[u.sub] || ''}
                      onChange={(e) =>
                        setUserModels((prev) => ({ ...prev, [u.sub]: e.target.value }))
                      }
                    >
                      <option value="">{t('models.useGlobalDefault')}</option>
                      {AVAILABLE_MODELS.map((m, i) =>
                        (m as any).disabled ? (
                          <option key={i} disabled>{m.label}</option>
                        ) : (
                          <option key={m.id} value={m.id}>{m.label}</option>
                        )
                      )}
                    </select>
                  </td>
                  <td className="cell-actions">
                    <button
                      className="btn btn-sm btn-primary"
                      onClick={() => handleSaveUserModel(u)}
                      disabled={(userModels[u.sub] || '') === (savedUserModels[u.sub] || '')}
                    >
                      {t('models.save')}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Memories Tab — standalone component
// ---------------------------------------------------------------------------
interface MemoriesTabProps {
  error: string;
  success: string;
  setError: (msg: string) => void;
  setSuccess: (msg: string) => void;
  clearMessages: () => void;
}

const MemoriesTab: React.FC<MemoriesTabProps> = ({ error, success, setError, clearMessages }) => {
  const [actors, setActors] = useState<string[]>([]);
  const [selectedActor, setSelectedActor] = useState<string | null>(null);
  const [records, setRecords] = useState<MemoryRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [recordsLoading, setRecordsLoading] = useState(false);
  const { t } = useI18n();

  const loadActors = useCallback(async () => {
    setLoading(true);
    try {
      const a = await listMemoryActors();
      setActors(a);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [setError]);

  useEffect(() => { loadActors(); }, [loadActors]);

  const handleSelectActor = async (actorId: string) => {
    clearMessages();
    setSelectedActor(actorId);
    setRecordsLoading(true);
    try {
      const r = await getMemoryRecords(actorId);
      setRecords(r);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setRecordsLoading(false);
    }
  };

  return (
    <div className="memories-section">
      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      <div className="toolbar">
        <div className="toolbar-left">
          <span className="toolbar-label">{t('memories.title')}</span>
        </div>
        <button className="btn btn-secondary btn-sm" onClick={() => { setSelectedActor(null); loadActors(); }}>
          {t('memories.refresh')}
        </button>
      </div>

      {!selectedActor && (
        <div className="skills-table-container">
          {loading ? (
            <div className="loading">{t('memories.loadingActors')}</div>
          ) : actors.length === 0 ? (
            <div className="empty-state">
              <p>{t('memories.noActors')}</p>
              <p className="empty-hint">{t('memories.noActorsHint')}</p>
            </div>
          ) : (
            <table className="skills-table">
              <thead>
                <tr>
                  <th>{t('memories.colActorId')}</th>
                  <th>{t('memories.colActions')}</th>
                </tr>
              </thead>
              <tbody>
                {actors.map((a) => (
                  <tr key={a}>
                    <td className="cell-name">{a}</td>
                    <td className="cell-actions">
                      <button className="btn btn-sm btn-primary" onClick={() => handleSelectActor(a)}>
                        {t('memories.viewMemories')}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {selectedActor && (
        <div>
          <div style={{ marginBottom: '16px' }}>
            <button className="btn btn-secondary btn-sm" onClick={() => setSelectedActor(null)}>
              {t('memories.backToActors')}
            </button>
            <span className="toolbar-label" style={{ marginLeft: '12px' }}>
              {t('memories.memoriesFor')} <code style={{ color: '#4a9eff' }}>{selectedActor}</code>
            </span>
          </div>

          <div className="skills-table-container">
            {recordsLoading ? (
              <div className="loading">{t('memories.loadingMemories')}</div>
            ) : records.length === 0 ? (
              <div className="empty-state">
                <p>{t('memories.noRecords')}</p>
              </div>
            ) : (
              <table className="skills-table">
                <thead>
                  <tr>
                    <th>{t('memories.colType')}</th>
                    <th>{t('memories.colContent')}</th>
                    <th>{t('memories.colCreated')}</th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((r) => (
                    <tr key={r.id}>
                      <td>
                        <span className={`badge ${r.type === 'facts' ? 'badge-active' : 'badge-admin'}`}>
                          {r.type}
                        </span>
                      </td>
                      <td className="cell-desc" style={{ maxWidth: '500px', whiteSpace: 'normal' }}>
                        {r.text}
                      </td>
                      <td className="cell-date">
                        {r.createdAt ? new Date(r.createdAt).toLocaleString() : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// Knowledge Base Tab — standalone component
// ---------------------------------------------------------------------------
interface KnowledgeBaseTabProps {
  error: string;
  success: string;
  setError: (msg: string) => void;
  setSuccess: (msg: string) => void;
  clearMessages: () => void;
  cognitoUsers: CognitoUserInfo[];
}

const KnowledgeBaseTab: React.FC<KnowledgeBaseTabProps> = ({
  error, success, setError, setSuccess, clearMessages, cognitoUsers,
}) => {
  const [scopes, setScopes] = useState<(string | KBScopeInfo)[]>([]);
  const [selectedScope, setSelectedScope] = useState('__shared__');
  const [documents, setDocuments] = useState<KBDocument[]>([]);
  const [loading, setLoading] = useState(false);
  const [docsLoading, setDocsLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [kbStatus, setKbStatus] = useState<string>('');
  const [syncJobs, setSyncJobs] = useState<KBSyncJob[]>([]);
  const { t } = useI18n();

  const displayScope = useCallback((scope: string): string => {
    if (scope === '__shared__') return t('kb.sharedScope');
    const match = cognitoUsers.find(
      (u) => u.email === scope || u.username === scope || u.sub === scope
    );
    return match?.email || match?.username || scope;
  }, [cognitoUsers, t]);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    try {
      const status = await getKBStatus();
      setKbStatus(status.status);
      // Build scope list: __shared__ + S3 scopes + Cognito users
      const scopeMap = new Map<string, number>();
      scopeMap.set('__shared__', 0);
      for (const s of status.scopes) {
        scopeMap.set(s.scope, s.documentCount);
      }
      // Add Cognito user emails as available scopes
      for (const u of cognitoUsers) {
        const email = u.email || u.username;
        if (email && !scopeMap.has(email)) {
          scopeMap.set(email, 0);
        }
      }
      setScopes(Array.from(scopeMap.entries()).map(([scope, documentCount]) => ({ scope, documentCount })));
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [setError, cognitoUsers]);

  const loadDocuments = useCallback(async (scope: string) => {
    setDocsLoading(true);
    try {
      const docs = await listKBDocuments(scope);
      setDocuments(docs);
    } catch (err: any) {
      setError(err.message);
      setDocuments([]);
    } finally {
      setDocsLoading(false);
    }
  }, [setError]);

  const loadSyncStatus = useCallback(async () => {
    try {
      const resp = await getKBSyncStatus();
      setSyncJobs(resp.jobs || []);
    } catch {
      setSyncJobs([]);
    }
  }, []);

  useEffect(() => {
    loadStatus();
    loadSyncStatus();
  }, [loadStatus, loadSyncStatus]);

  useEffect(() => {
    loadDocuments(selectedScope);
  }, [selectedScope, loadDocuments]);

  const handleUpload = async (file: File) => {
    setUploading(true);
    clearMessages();
    try {
      const { uploadUrl } = await getKBUploadUrl(
        selectedScope,
        file.name,
        file.type || 'application/octet-stream'
      );
      await uploadKBDocument(uploadUrl, file);
      setSuccess(t('kb.docUploaded').replace('{name}', file.name).replace('{scope}', displayScope(selectedScope)));
      loadDocuments(selectedScope);
      loadStatus();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (key: string) => {
    clearMessages();
    try {
      await deleteKBDocument(key);
      setSuccess(t('kb.docDeleted'));
      loadDocuments(selectedScope);
      loadStatus();
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleSync = async () => {
    setSyncing(true);
    clearMessages();
    try {
      await startKBSync();
      setSuccess(t('kb.syncStarted'));
      loadSyncStatus();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSyncing(false);
    }
  };

  const handleAddScope = () => {
    const email = prompt(t('kb.promptScope'));
    if (email && email.trim()) {
      const trimmed = email.trim();
      const existing = (scopes as KBScopeInfo[]).map(s => typeof s === 'string' ? s : s.scope);
      if (!existing.includes(trimmed)) {
        setScopes(prev => [...prev, { scope: trimmed, documentCount: 0 } as KBScopeInfo]);
      }
      setSelectedScope(trimmed);
    }
  };

  const scopeItems = (scopes as KBScopeInfo[]).map(s => typeof s === 'string' ? { scope: s, documentCount: 0 } : s);
  // Ensure __shared__ is always first
  if (!scopeItems.find(s => s.scope === '__shared__')) {
    scopeItems.unshift({ scope: '__shared__', documentCount: 0 });
  }

  return (
    <div className="kb-section">
      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      <div className="settings-panel" style={{ marginBottom: '16px' }}>
        <h3 style={{ color: '#fff', margin: '0 0 8px 0', fontSize: '16px' }}>{t('kb.title')}</h3>
        <p className="settings-hint" style={{ margin: '0 0 12px 0' }}>
          {t('kb.desc')}
        </p>
        <div className="settings-row">
          <span className="toolbar-label">{t('kb.status')}</span>
          <span className={`badge ${kbStatus === 'ACTIVE' ? 'badge-active' : kbStatus === 'NOT_INITIALIZED' ? 'badge-inactive' : 'badge-group'}`}>
            {kbStatus === 'ACTIVE' ? t('kb.statusActive') : kbStatus === 'NOT_INITIALIZED' ? t('kb.statusNotInit') : kbStatus}
          </span>
        </div>
      </div>

      {/* Scope summary cards */}
      {scopeItems.length > 0 && (
        <div className="settings-panel" style={{ marginBottom: '16px' }}>
          <h3 style={{ color: '#fff', margin: '0 0 8px 0', fontSize: '14px' }}>{t('kb.scopeSummary')}</h3>
          <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
            {scopeItems.map((s) => (
              <button
                key={s.scope}
                className={`btn btn-sm ${selectedScope === s.scope ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setSelectedScope(s.scope)}
                style={{ minWidth: '120px' }}
              >
                {displayScope(s.scope)} ({s.documentCount} {t('kb.documents')})
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Toolbar */}
      <div className="toolbar">
        <div className="toolbar-left">
          <label className="toolbar-label">{t('kb.scope')}</label>
          <select
            className="toolbar-select"
            value={selectedScope}
            onChange={(e) => setSelectedScope(e.target.value)}
          >
            {scopeItems.map((s) => (
              <option key={s.scope} value={s.scope}>
                {displayScope(s.scope)}
              </option>
            ))}
          </select>
          <button className="btn btn-secondary btn-sm" onClick={handleAddScope}>
            {t('kb.addScope')}
          </button>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <label className="file-upload-btn btn btn-primary btn-sm" style={{ marginBottom: 0 }}>
            {uploading ? t('kb.uploading') : t('kb.uploadDoc')}
            <input
              type="file"
              hidden
              disabled={uploading}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) {
                  handleUpload(file);
                  e.target.value = '';
                }
              }}
            />
          </label>
          <button
            className="btn btn-secondary btn-sm"
            onClick={handleSync}
            disabled={syncing}
          >
            {syncing ? t('kb.syncing') : t('kb.sync')}
          </button>
          <button className="btn btn-secondary btn-sm" onClick={() => { loadStatus(); loadDocuments(selectedScope); loadSyncStatus(); }}>
            {t('kb.refresh')}
          </button>
        </div>
      </div>

      {/* Document list */}
      <div className="skills-table-container">
        {docsLoading ? (
          <div className="loading">{t('files.loading')}</div>
        ) : documents.length === 0 ? (
          <div className="empty-state">
            <p>{kbStatus === 'NOT_INITIALIZED' ? t('kb.notInitialized') : t('kb.noDocuments')}</p>
            <p className="empty-hint">{t('kb.noDocumentsHint')}</p>
          </div>
        ) : (
          <table className="skills-table">
            <thead>
              <tr>
                <th>{t('kb.colName')}</th>
                <th>{t('kb.colSize')}</th>
                <th>{t('kb.colModified')}</th>
                <th>{t('kb.colActions')}</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <tr key={doc.key}>
                  <td className="cell-name">{doc.name}</td>
                  <td className="cell-date">
                    {doc.size < 1024
                      ? `${doc.size} B`
                      : doc.size < 1048576
                        ? `${(doc.size / 1024).toFixed(1)} KB`
                        : `${(doc.size / 1048576).toFixed(1)} MB`}
                  </td>
                  <td className="cell-date">
                    {new Date(doc.lastModified).toLocaleDateString()}
                  </td>
                  <td className="cell-actions">
                    <button
                      className="btn btn-sm btn-danger"
                      onClick={() => handleDelete(doc.key)}
                    >
                      {t('kb.delete')}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Sync status */}
      {syncJobs.length > 0 && (
        <div style={{ marginTop: '16px' }}>
          <div className="toolbar">
            <div className="toolbar-left">
              <span className="toolbar-label">{t('kb.syncStatus')}</span>
            </div>
          </div>
          <div className="skills-table-container">
            <table className="skills-table">
              <thead>
                <tr>
                  <th>{t('kb.syncJobStatus')}</th>
                  <th>{t('kb.syncJobStarted')}</th>
                  <th>{t('kb.syncJobUpdated')}</th>
                </tr>
              </thead>
              <tbody>
                {syncJobs.map((job) => (
                  <tr key={job.ingestionJobId}>
                    <td>
                      <span className={`badge ${
                        job.status === 'COMPLETE' ? 'badge-active' :
                        job.status === 'IN_PROGRESS' || job.status === 'STARTING' ? 'badge-group' :
                        'badge-inactive'
                      }`}>
                        {job.status}
                      </span>
                    </td>
                    <td className="cell-date">
                      {job.startedAt ? new Date(job.startedAt).toLocaleString() : '-'}
                    </td>
                    <td className="cell-date">
                      {job.updatedAt ? new Date(job.updatedAt).toLocaleString() : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// Main AdminConsole component
// ---------------------------------------------------------------------------
const AdminConsole: React.FC = () => {
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [userIds, setUserIds] = useState<string[]>(['__global__']);
  const [selectedUserId, setSelectedUserId] = useState('__global__');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const { t } = useI18n();

  // Form state
  const [showForm, setShowForm] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [form, setForm] = useState<SkillFormData>(emptyForm);

  // Delete confirmation
  const [deleteTarget, setDeleteTarget] = useState<SkillItem | null>(null);

  // User settings (model ID)
  const [modelId, setModelId] = useState('');
  const [savedModelId, setSavedModelId] = useState('');

  // Tabs
  const [activeTab, setActiveTab] = useState<'skills' | 'knowledgeBase' | 'models' | 'sessions' | 'users' | 'integrations' | 'memories' | 'guardrails'>('skills');

  // Sessions
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);

  // Users tab
  const [cognitoUsers, setCognitoUsers] = useState<CognitoUserInfo[]>([]);
  const [gatewayTools, setGatewayTools] = useState<GatewayTool[]>([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [selectedPermUser, setSelectedPermUser] = useState<CognitoUserInfo | null>(null);
  const [userToolSelections, setUserToolSelections] = useState<Record<string, boolean>>({});
  const [permSaving, setPermSaving] = useState(false);
  const [policyMode, setPolicyMode] = useState<'ENFORCE' | 'LOG_ONLY'>('ENFORCE');
  const [policyModeSaving, setPolicyModeSaving] = useState(false);
  const [permOriginal, setPermOriginal] = useState<string[]>([]);

  // File manager (shown when editing a skill)
  const [skillFiles, setSkillFiles] = useState<SkillFile[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [expandedDirs, setExpandedDirs] = useState<Record<string, boolean>>({
    scripts: true,
    references: true,
    assets: true,
  });

  const loadSkills = useCallback(async () => {
    setIsLoading(true);
    setError('');
    try {
      const items = await listSkills(selectedUserId);
      setSkills(items);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setIsLoading(false);
    }
  }, [selectedUserId]);

  const loadSettings = useCallback(async () => {
    try {
      const s = await getSettings(selectedUserId);
      setModelId(s.modelId || '');
      setSavedModelId(s.modelId || '');
    } catch {
      setModelId('');
      setSavedModelId('');
    }
  }, [selectedUserId]);

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const items = await listSessions();
      setSessions(items);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  const loadSkillFiles = useCallback(async (userId: string, skillName: string) => {
    setFilesLoading(true);
    try {
      const files = await listSkillFiles(userId, skillName);
      setSkillFiles(files);
    } catch {
      setSkillFiles([]);
    } finally {
      setFilesLoading(false);
    }
  }, []);

  const handleFileUpload = async (directory: string, file: File) => {
    if (!isEditing) return;
    setUploading(true);
    clearMessages();
    try {
      const { uploadUrl } = await getUploadUrl(
        form.userId,
        form.skillName,
        directory,
        file.name,
        file.type || 'application/octet-stream'
      );
      await uploadSkillFile(uploadUrl, file);
      setSuccess(t('files.fileUploaded').replace('{name}', file.name).replace('{dir}', directory));
      loadSkillFiles(form.userId, form.skillName);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setUploading(false);
    }
  };

  const handleFileDownload = async (filePath: string) => {
    clearMessages();
    try {
      const url = await getDownloadUrl(form.userId, form.skillName, filePath);
      window.open(url, '_blank');
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleFileDelete = async (filePath: string) => {
    clearMessages();
    try {
      await deleteSkillFile(form.userId, form.skillName, filePath);
      setSuccess(t('files.fileDeleted').replace('{path}', filePath));
      loadSkillFiles(form.userId, form.skillName);
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleStopSession = async (sessionId: string) => {
    clearMessages();
    try {
      await stopSession(sessionId);
      setSuccess(t('sessions.stopRequested').replace('{id}', sessionId));
      loadSessions();
    } catch (err: any) {
      setError(err.message);
    }
  };

  // Resolve a userId (sub, email, or username) to a display-friendly email
  const displayUserId = useCallback((id: string): string => {
    if (id === '__global__') return t('skills.globalAll');
    // Try matching by sub, email, or username
    const match = cognitoUsers.find(
      (u) => u.sub === id || u.email === id || u.username === id
    );
    return match?.email || match?.username || id;
  }, [cognitoUsers, t]);

  // Users tab loaders
  const loadCognitoUsers = useCallback(async () => {
    setUsersLoading(true);
    try {
      const [users, tools] = await Promise.all([listCognitoUsers(), listGatewayTools()]);
      setCognitoUsers(users);
      setGatewayTools(tools);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setUsersLoading(false);
    }
  }, []);

  // Cedar principal.id maps to JWT sub claim (Cognito sub UUID)
  const getActorId = (user: CognitoUserInfo) => user.sub;

  const handleManagePermissions = async (user: CognitoUserInfo) => {
    clearMessages();
    setSelectedPermUser(user);
    try {
      const perms = await getUserPermissions(getActorId(user));
      const allowed = perms.allowedTools || [];
      setPermOriginal(allowed);
      const selections: Record<string, boolean> = {};
      for (const tool of gatewayTools) {
        selections[tool.name] = allowed.includes(tool.name);
      }
      setUserToolSelections(selections);
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleSavePermissions = async () => {
    if (!selectedPermUser) return;
    setPermSaving(true);
    clearMessages();
    try {
      const selectedTools = Object.entries(userToolSelections)
        .filter(([, checked]) => checked)
        .map(([name]) => name);
      await updateUserPermissions(getActorId(selectedPermUser), selectedTools);
      setPermOriginal(selectedTools);
      setSuccess(t('users.permsUpdated').replace('{user}', selectedPermUser.email || selectedPermUser.username || ''));
    } catch (err: any) {
      setError(err.message);
    } finally {
      setPermSaving(false);
    }
  };

  const handleCancelPermissions = () => {
    setSelectedPermUser(null);
    setUserToolSelections({});
    setPermOriginal([]);
  };

  const permIsDirty = (() => {
    const current = Object.entries(userToolSelections)
      .filter(([, v]) => v)
      .map(([k]) => k)
      .sort();
    const orig = [...permOriginal].sort();
    return JSON.stringify(current) !== JSON.stringify(orig);
  })();

  const handleSaveSettings = async () => {
    clearMessages();
    try {
      await updateSettings(selectedUserId, { modelId });
      setSavedModelId(modelId);
      setSuccess(t('users.modelUpdated').replace('{user}', selectedUserId === '__global__' ? 'default (all users)' : selectedUserId));
    } catch (err: any) {
      setError(err.message);
    }
  };

  const loadUserIds = useCallback(async () => {
    try {
      const ids = await listUsers();
      // Always ensure __global__ is the first entry
      const withGlobal = ids.includes('__global__') ? ids : ['__global__', ...ids];
      setUserIds(withGlobal);
    } catch {
      // Ignore — user list is optional
    }
  }, []);

  useEffect(() => {
    loadSkills();
    loadSettings();
  }, [loadSkills, loadSettings]);

  useEffect(() => {
    loadUserIds();
    // Load Cognito users early so we can resolve sub → email everywhere
    listCognitoUsers().then((users) => setCognitoUsers(users)).catch(() => {});
  }, [loadUserIds]);

  useEffect(() => {
    if (activeTab === 'sessions') {
      loadSessions();
    }
    if (activeTab === 'users') {
      loadCognitoUsers();
    }
    if (activeTab === 'models') {
      loadUserIds();
      loadSettings();
    }
  }, [activeTab, loadSessions, loadCognitoUsers, loadUserIds, loadSettings]);

  const clearMessages = () => {
    setError('');
    setSuccess('');
  };

  const handleCreate = () => {
    clearMessages();
    setForm({ ...emptyForm, userId: selectedUserId });
    setIsEditing(false);
    setShowForm(true);
  };

  const handleEdit = (skill: SkillItem) => {
    clearMessages();
    const metadataEntries: MetadataEntry[] = skill.metadata
      ? Object.entries(skill.metadata).map(([key, value]) => ({ key, value }))
      : [];
    setForm({
      userId: skill.userId,
      skillName: skill.skillName,
      description: skill.description,
      instructions: skill.instructions,
      allowedTools: (skill.allowedTools || []).join(', '),
      license: skill.license || '',
      compatibility: skill.compatibility || '',
      metadata: metadataEntries,
    });
    setIsEditing(true);
    setShowForm(true);
    loadSkillFiles(skill.userId, skill.skillName);
  };

  const handleCancel = () => {
    setShowForm(false);
    setForm(emptyForm);
    setSkillFiles([]);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    clearMessages();

    if (!isEditing && !SKILL_NAME_RE.test(form.skillName)) {
      setError(t('skills.invalidName'));
      return;
    }

    if (!form.description.trim()) {
      setError(t('skills.descriptionRequired'));
      return;
    }

    const allowedTools = form.allowedTools
      .split(',')
      .map((tt) => tt.trim())
      .filter(Boolean);

    const metadata: Record<string, string> = {};
    for (const entry of form.metadata) {
      if (entry.key.trim()) {
        metadata[entry.key.trim()] = entry.value;
      }
    }

    try {
      if (isEditing) {
        await updateSkill(form.userId, form.skillName, {
          description: form.description,
          instructions: form.instructions,
          allowedTools,
          license: form.license,
          compatibility: form.compatibility,
          metadata,
        });
        setSuccess(t('skills.skillUpdated').replace('{name}', form.skillName));
      } else {
        const input: SkillInput = {
          userId: form.userId,
          skillName: form.skillName,
          description: form.description,
          instructions: form.instructions,
          allowedTools,
          license: form.license || undefined,
          compatibility: form.compatibility || undefined,
          metadata: Object.keys(metadata).length > 0 ? metadata : undefined,
        };
        await createSkill(input);
        setSuccess(t('skills.skillCreated').replace('{name}', form.skillName));
      }
      setShowForm(false);
      setForm(emptyForm);
      setSkillFiles([]);
      loadSkills();
      loadUserIds();
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    clearMessages();
    try {
      await deleteSkill(deleteTarget.userId, deleteTarget.skillName);
      setSuccess(t('skills.skillDeleted').replace('{name}', deleteTarget.skillName));
      setDeleteTarget(null);
      loadSkills();
      loadUserIds();
    } catch (err: any) {
      setError(err.message);
      setDeleteTarget(null);
    }
  };

  const handleAddUserScope = () => {
    const newUser = prompt(t('skills.promptUserId'));
    if (newUser && newUser.trim()) {
      const trimmed = newUser.trim();
      if (!userIds.includes(trimmed)) {
        setUserIds((prev) => [...prev, trimmed]);
      }
      setSelectedUserId(trimmed);
    }
  };

  return (
    <div className="admin-console">
      {/* Tabs */}
      <div className="tab-bar">
        <button
          className={`tab ${activeTab === 'skills' ? 'tab-active' : ''}`}
          onClick={() => setActiveTab('skills')}
        >
          {t('tab.skills')}
        </button>
        <button
          className={`tab ${activeTab === 'knowledgeBase' ? 'tab-active' : ''}`}
          onClick={() => setActiveTab('knowledgeBase')}
        >
          {t('tab.knowledgeBase')}
        </button>
        <button
          className={`tab ${activeTab === 'models' ? 'tab-active' : ''}`}
          onClick={() => setActiveTab('models')}
        >
          {t('tab.models')}
        </button>
        <button
          className={`tab ${activeTab === 'users' ? 'tab-active' : ''}`}
          onClick={() => setActiveTab('users')}
        >
          {t('tab.toolAccess')}
        </button>
        <button
          className={`tab ${activeTab === 'integrations' ? 'tab-active' : ''}`}
          onClick={() => setActiveTab('integrations')}
        >
          {t('tab.integrations')}
        </button>
        <button
          className={`tab ${activeTab === 'sessions' ? 'tab-active' : ''}`}
          onClick={() => setActiveTab('sessions')}
        >
          {t('tab.sessions')}
        </button>
        <button
          className={`tab ${activeTab === 'memories' ? 'tab-active' : ''}`}
          onClick={() => setActiveTab('memories')}
        >
          {t('tab.memories')}
        </button>
        <button
          className={`tab ${activeTab === 'guardrails' ? 'tab-active' : ''}`}
          onClick={() => setActiveTab('guardrails')}
        >
          {t('tab.guardrails')}
        </button>
      </div>

      {activeTab === 'skills' && (
      <>
      {/* Toolbar */}
      <div className="toolbar">
        <div className="toolbar-left">
          <label className="toolbar-label">{t('skills.userScope')}</label>
          <select
            className="toolbar-select"
            value={selectedUserId}
            onChange={(e) => setSelectedUserId(e.target.value)}
          >
            {userIds.map((id) => (
              <option key={id} value={id}>
                {displayUserId(id)}
              </option>
            ))}
          </select>
          <button className="btn btn-secondary btn-sm" onClick={handleAddUserScope}>
            {t('skills.addUser')}
          </button>
        </div>
        <button className="btn btn-primary" onClick={handleCreate}>
          {t('skills.createSkill')}
        </button>
      </div>

      {/* Messages */}
      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      {/* Delete confirmation modal */}
      {deleteTarget && (
        <div className="modal-overlay" onClick={() => setDeleteTarget(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>{t('skills.deleteSkill')}</h3>
            <p>
              {t('skills.deleteConfirm')} <strong>{deleteTarget.skillName}</strong> {t('skills.forUserScope')} <strong>{displayUserId(deleteTarget.userId)}</strong>?
            </p>
            <div className="modal-actions">
              <button className="btn btn-secondary" onClick={() => setDeleteTarget(null)}>
                {t('skills.cancel')}
              </button>
              <button className="btn btn-danger" onClick={handleDelete}>
                {t('skills.delete')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Skill Form */}
      {showForm && (
        <div className="skill-form-container">
          <h3>{isEditing ? t('skills.editSkill') : t('skills.createSkillTitle')}</h3>
          <form onSubmit={handleSubmit}>
            <div className="form-row">
              <div className="form-group">
                <label>{t('skills.userScopeLabel')}</label>
                <input
                  type="text"
                  value={isEditing ? displayUserId(form.userId) : form.userId}
                  onChange={(e) => setForm({ ...form, userId: e.target.value })}
                  disabled={isEditing}
                  placeholder="__global__"
                />
              </div>
              <div className="form-group">
                <label>{t('skills.skillName')}</label>
                <input
                  type="text"
                  value={form.skillName}
                  onChange={(e) =>
                    setForm({ ...form, skillName: e.target.value.toLowerCase() })
                  }
                  disabled={isEditing}
                  placeholder={t('skills.skillNamePlaceholder')}
                />
              </div>
            </div>
            <div className="form-group">
              <label>{t('skills.description')} <span className="field-required">*</span></label>
              <input
                type="text"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder={t('skills.descriptionPlaceholder')}
                maxLength={1024}
              />
            </div>
            <div className="form-group">
              <label>{t('skills.allowedTools')}</label>
              <input
                type="text"
                value={form.allowedTools}
                onChange={(e) => setForm({ ...form, allowedTools: e.target.value })}
                placeholder={t('skills.allowedToolsPlaceholder')}
              />
            </div>

            <div className="form-section-label">{t('skills.optionalFields')}</div>
            <div className="form-row">
              <div className="form-group">
                <label>{t('skills.license')}</label>
                <input
                  type="text"
                  value={form.license}
                  onChange={(e) => setForm({ ...form, license: e.target.value })}
                  placeholder={t('skills.licensePlaceholder')}
                />
              </div>
              <div className="form-group">
                <label>{t('skills.compatibility')}</label>
                <input
                  type="text"
                  value={form.compatibility}
                  onChange={(e) => setForm({ ...form, compatibility: e.target.value })}
                  placeholder={t('skills.compatibilityPlaceholder')}
                  maxLength={500}
                />
              </div>
            </div>

            <div className="form-group">
              <label>{t('skills.metadata')}</label>
              <div className="metadata-editor">
                {form.metadata.map((entry, i) => (
                  <div key={i} className="metadata-row">
                    <input
                      type="text"
                      className="metadata-key"
                      value={entry.key}
                      onChange={(e) => {
                        const updated = [...form.metadata];
                        updated[i] = { ...updated[i], key: e.target.value };
                        setForm({ ...form, metadata: updated });
                      }}
                      placeholder={t('skills.metadataKey')}
                    />
                    <input
                      type="text"
                      className="metadata-value"
                      value={entry.value}
                      onChange={(e) => {
                        const updated = [...form.metadata];
                        updated[i] = { ...updated[i], value: e.target.value };
                        setForm({ ...form, metadata: updated });
                      }}
                      placeholder={t('skills.metadataValue')}
                    />
                    <button
                      type="button"
                      className="btn btn-sm btn-danger metadata-remove"
                      onClick={() => {
                        const updated = form.metadata.filter((_, idx) => idx !== i);
                        setForm({ ...form, metadata: updated });
                      }}
                    >
                      {t('skills.remove')}
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="btn btn-sm btn-secondary"
                  onClick={() =>
                    setForm({
                      ...form,
                      metadata: [...form.metadata, { key: '', value: '' }],
                    })
                  }
                >
                  {t('skills.addEntry')}
                </button>
              </div>
            </div>

            <div className="form-group">
              <label>{t('skills.instructions')}</label>
              <textarea
                className="instructions-textarea"
                value={form.instructions}
                onChange={(e) => setForm({ ...form, instructions: e.target.value })}
                placeholder={t('skills.instructionsPlaceholder')}
                rows={12}
              />
            </div>

            <div className="form-actions">
              <button type="button" className="btn btn-secondary" onClick={handleCancel}>
                {t('skills.cancel')}
              </button>
              <button type="submit" className="btn btn-primary">
                {isEditing ? t('skills.saveChanges') : t('skills.createSkill')}
              </button>
            </div>
          </form>

          {/* File Manager (only when editing) */}
          {isEditing && (
            <div className="file-manager">
              <h4>{t('files.title')}</h4>
              <p className="file-manager-hint">
                {t('files.hint')}
              </p>
              {filesLoading ? (
                <div className="loading">{t('files.loading')}</div>
              ) : (
                ['scripts', 'references', 'assets'].map((dir) => {
                  const dirFiles = skillFiles.filter((f) => f.path.startsWith(dir + '/'));
                  return (
                    <div key={dir} className="file-dir-section">
                      <button
                        type="button"
                        className="file-dir-header"
                        onClick={() =>
                          setExpandedDirs((prev) => ({ ...prev, [dir]: !prev[dir] }))
                        }
                      >
                        <span className="file-dir-arrow">
                          {expandedDirs[dir] ? '\u25BE' : '\u25B8'}
                        </span>
                        <span className="file-dir-name">{dir}/</span>
                        <span className="file-dir-count">
                          {dirFiles.length} file{dirFiles.length !== 1 ? 's' : ''}
                        </span>
                      </button>
                      {expandedDirs[dir] && (
                        <div className="file-dir-body">
                          {dirFiles.length > 0 && (
                            <table className="file-table">
                              <thead>
                                <tr>
                                  <th>{t('files.colName')}</th>
                                  <th>{t('files.colSize')}</th>
                                  <th>{t('files.colModified')}</th>
                                  <th>{t('files.colActions')}</th>
                                </tr>
                              </thead>
                              <tbody>
                                {dirFiles.map((f) => {
                                  const name = f.path.split('/').pop() || f.path;
                                  return (
                                    <tr key={f.path}>
                                      <td className="cell-name">{name}</td>
                                      <td className="cell-date">
                                        {f.size < 1024
                                          ? `${f.size} B`
                                          : `${(f.size / 1024).toFixed(1)} KB`}
                                      </td>
                                      <td className="cell-date">
                                        {new Date(f.lastModified).toLocaleDateString()}
                                      </td>
                                      <td className="cell-actions">
                                        <button
                                          type="button"
                                          className="btn btn-sm btn-secondary"
                                          onClick={() => handleFileDownload(f.path)}
                                        >
                                          {t('files.download')}
                                        </button>
                                        <button
                                          type="button"
                                          className="btn btn-sm btn-danger"
                                          onClick={() => handleFileDelete(f.path)}
                                        >
                                          {t('files.delete')}
                                        </button>
                                      </td>
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          )}
                          <label className="file-upload-btn btn btn-sm btn-secondary">
                            {uploading ? t('files.uploading') : t('files.uploadTo').replace('{dir}', dir)}
                            <input
                              type="file"
                              hidden
                              disabled={uploading}
                              onChange={(e) => {
                                const file = e.target.files?.[0];
                                if (file) {
                                  handleFileUpload(dir, file);
                                  e.target.value = '';
                                }
                              }}
                            />
                          </label>
                        </div>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          )}
        </div>
      )}

      {/* Skills Table */}
      {!showForm && (
        <div className="skills-table-container">
          {isLoading ? (
            <div className="loading">{t('skills.loading')}</div>
          ) : skills.length === 0 ? (
            <div className="empty-state">
              <p>{t('skills.noSkills')}</p>
              <p className="empty-hint">{t('skills.noSkillsHint')}</p>
            </div>
          ) : (
            <table className="skills-table">
              <thead>
                <tr>
                  <th>{t('skills.colName')}</th>
                  <th>{t('skills.colDescription')}</th>
                  <th>{t('skills.colTools')}</th>
                  <th>{t('skills.colUpdated')}</th>
                  <th>{t('skills.colActions')}</th>
                </tr>
              </thead>
              <tbody>
                {skills.map((skill) => (
                  <tr key={`${skill.userId}:${skill.skillName}`}>
                    <td className="cell-name">{skill.skillName}</td>
                    <td className="cell-desc">{skill.description}</td>
                    <td className="cell-tools">
                      {(skill.allowedTools || []).join(', ') || '-'}
                    </td>
                    <td className="cell-date">
                      {skill.updatedAt
                        ? new Date(skill.updatedAt).toLocaleDateString()
                        : '-'}
                    </td>
                    <td className="cell-actions">
                      <button className="btn btn-sm btn-secondary" onClick={() => handleEdit(skill)}>
                        {t('skills.edit')}
                      </button>
                      <button
                        className="btn btn-sm btn-danger"
                        onClick={() => setDeleteTarget(skill)}
                      >
                        {t('skills.delete')}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      </>
      )}

      {/* Knowledge Base Tab */}
      {activeTab === 'knowledgeBase' && (
        <KnowledgeBaseTab
          error={error}
          success={success}
          clearMessages={clearMessages}
          setError={setError}
          setSuccess={setSuccess}
          cognitoUsers={cognitoUsers}
        />
      )}

      {/* Models Tab */}
      {activeTab === 'models' && (
        <ModelsTab
          error={error}
          success={success}
          clearMessages={clearMessages}
          setError={setError}
          setSuccess={setSuccess}
        />
      )}

      {/* Sessions Tab */}
      {activeTab === 'sessions' && (
        <div className="sessions-section">
          {error && <div className="alert alert-error">{error}</div>}
          {success && <div className="alert alert-success">{success}</div>}

          <div className="toolbar">
            <div className="toolbar-left">
              <span className="toolbar-label">{t('sessions.title')}</span>
            </div>
            <button className="btn btn-secondary btn-sm" onClick={loadSessions}>
              {t('sessions.refresh')}
            </button>
          </div>

          <div className="skills-table-container">
            {sessionsLoading ? (
              <div className="loading">{t('sessions.loading')}</div>
            ) : sessions.length === 0 ? (
              <div className="empty-state">
                <p>{t('sessions.noSessions')}</p>
                <p className="empty-hint">{t('sessions.noSessionsHint')}</p>
              </div>
            ) : (
              <table className="skills-table">
                <thead>
                  <tr>
                    <th>{t('sessions.colUserId')}</th>
                    <th>{t('sessions.colSessionId')}</th>
                    <th>{t('sessions.colLastActive')}</th>
                    <th>{t('sessions.colActions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((s) => (
                    <tr key={s.sessionId}>
                      <td className="cell-name" title={s.userId}>
                        {s.userId.length > 24 ? s.userId.slice(0, 24) + '...' : s.userId}
                      </td>
                      <td className="cell-tools" title={s.sessionId}>
                        {s.sessionId.length > 36 ? s.sessionId.slice(0, 36) + '...' : s.sessionId}
                      </td>
                      <td className="cell-date">
                        {s.lastActiveAt ? new Date(s.lastActiveAt).toLocaleString() : '-'}
                      </td>
                      <td className="cell-actions">
                        <button
                          className="btn btn-sm btn-danger"
                          onClick={() => handleStopSession(s.sessionId)}
                        >
                          {t('sessions.stop')}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      {/* Tool Access Tab */}
      {activeTab === 'users' && (
        <div className="users-section">
          {error && <div className="alert alert-error">{error}</div>}
          {success && <div className="alert alert-success">{success}</div>}

          <div className="settings-panel">
            <div className="settings-row">
              <label className="toolbar-label">{t('users.policyEngine')}</label>
              <select
                className="toolbar-select"
                style={{ minWidth: '140px' }}
                value={policyMode}
                onChange={(e) => setPolicyMode(e.target.value as 'ENFORCE' | 'LOG_ONLY')}
              >
                <option value="ENFORCE">{t('users.enforce')}</option>
                <option value="LOG_ONLY">{t('users.logOnly')}</option>
              </select>
              <span className={`badge ${policyMode === 'ENFORCE' ? 'badge-active' : 'badge-inactive'}`}>
                {policyMode === 'ENFORCE' ? t('users.enforced') : t('users.auditOnly')}
              </span>
            </div>
          </div>

          <div className="toolbar">
            <div className="toolbar-left">
              <span className="toolbar-label">{t('users.perUserPerms')}</span>
            </div>
            <button className="btn btn-secondary btn-sm" onClick={loadCognitoUsers}>
              {t('users.refresh')}
            </button>
          </div>

          {selectedPermUser && (
            <div className="perm-editor">
              <h3>
                {t('users.toolPermsFor')}{' '}
                <span className="perm-user-email">
                  {selectedPermUser.email || selectedPermUser.username}
                </span>
              </h3>
              <p className="perm-hint">
                {t('users.toolPermsHint')}{' '}
                {t('users.actorId')} <code>{getActorId(selectedPermUser)}</code>
              </p>

              {gatewayTools.length === 0 ? (
                <div className="empty-state">
                  <p>{t('users.noTools')}</p>
                </div>
              ) : (
                <>
                  <div className="perm-select-bar">
                    <button
                      type="button"
                      className="btn btn-sm btn-secondary"
                      onClick={() => {
                        const all: Record<string, boolean> = {};
                        for (const tt of gatewayTools) all[tt.name] = true;
                        setUserToolSelections(all);
                      }}
                    >
                      {t('users.selectAll')}
                    </button>
                    <button
                      type="button"
                      className="btn btn-sm btn-secondary"
                      onClick={() => {
                        const none: Record<string, boolean> = {};
                        for (const tt of gatewayTools) none[tt.name] = false;
                        setUserToolSelections(none);
                      }}
                    >
                      {t('users.deselectAll')}
                    </button>
                  </div>
                  <div className="perm-tool-list">
                    {gatewayTools.map((tool) => (
                      <label key={tool.name} className="perm-tool-item">
                        <input
                          type="checkbox"
                          className="perm-tool-checkbox"
                          checked={!!userToolSelections[tool.name]}
                          onChange={(e) =>
                            setUserToolSelections((prev) => ({
                              ...prev,
                              [tool.name]: e.target.checked,
                            }))
                          }
                        />
                        <span className="perm-tool-name">{tool.name}</span>
                        <span className="perm-tool-desc">{tool.description}</span>
                        <span className="perm-tool-target">{tool.targetName}</span>
                      </label>
                    ))}
                  </div>
                </>
              )}

              <div className="form-actions">
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={handleCancelPermissions}
                >
                  {t('users.cancel')}
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleSavePermissions}
                  disabled={!permIsDirty || permSaving}
                >
                  {permSaving ? t('users.saving') : t('users.savePermissions')}
                </button>
              </div>
            </div>
          )}

          {!selectedPermUser && (
            <div className="skills-table-container">
              {usersLoading ? (
                <div className="loading">{t('users.loadingUsers')}</div>
              ) : cognitoUsers.length === 0 ? (
                <div className="empty-state">
                  <p>{t('users.noUsers')}</p>
                </div>
              ) : (
                <table className="skills-table">
                  <thead>
                    <tr>
                      <th>{t('users.colEmail')}</th>
                      <th>{t('users.colUserId')}</th>
                      <th>{t('users.colStatus')}</th>
                      <th>{t('users.colGroups')}</th>
                      <th>{t('users.colActions')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cognitoUsers.map((u) => (
                      <tr key={u.sub}>
                        <td className="cell-name">{u.email || u.username}</td>
                        <td className="cell-tools" title={u.sub}>
                          {u.sub.length > 28 ? u.sub.slice(0, 28) + '...' : u.sub}
                        </td>
                        <td>
                          <span
                            className={`badge ${
                              u.status === 'CONFIRMED'
                                ? 'badge-active'
                                : 'badge-inactive'
                            }`}
                          >
                            {u.status}
                          </span>
                        </td>
                        <td>
                          {u.groups.length > 0
                            ? u.groups.map((g) => (
                                <span
                                  key={g}
                                  className={`badge ${
                                    g === 'admin' ? 'badge-admin' : 'badge-group'
                                  }`}
                                >
                                  {g}
                                </span>
                              ))
                            : '-'}
                        </td>
                        <td className="cell-actions">
                          <button
                            className="btn btn-sm btn-primary"
                            onClick={() => handleManagePermissions(u)}
                          >
                            {t('users.managePermissions')}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      )}

      {/* Integrations Tab */}
      {activeTab === 'integrations' && (
        <div className="integrations-section">
          <div className="settings-panel" style={{ marginBottom: '16px' }}>
            <h3 style={{ color: '#fff', margin: '0 0 12px 0', fontSize: '16px' }}>{t('integrations.title')}</h3>
            <p className="settings-hint" style={{ margin: '0 0 16px 0' }}>
              {t('integrations.desc')}
            </p>
          </div>

          <div className="skills-table-container">
            <table className="skills-table">
              <thead>
                <tr>
                  <th>{t('integrations.colType')}</th>
                  <th>{t('integrations.colDescription')}</th>
                  <th>{t('integrations.colStatus')}</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="cell-name">{t('integrations.lambdaTargets')}</td>
                  <td className="cell-desc">{t('integrations.lambdaDesc')}</td>
                  <td><span className="badge badge-active">{t('integrations.active')}</span></td>
                </tr>
                <tr>
                  <td className="cell-name">{t('integrations.mcpServers')}</td>
                  <td className="cell-desc">{t('integrations.mcpDesc')}</td>
                  <td><span className="badge badge-inactive">{t('integrations.planned')}</span></td>
                </tr>
                <tr>
                  <td className="cell-name">{t('integrations.a2aAgents')}</td>
                  <td className="cell-desc">{t('integrations.a2aDesc')}</td>
                  <td><span className="badge badge-inactive">{t('integrations.planned')}</span></td>
                </tr>
                <tr>
                  <td className="cell-name">{t('integrations.apiGateway')}</td>
                  <td className="cell-desc">{t('integrations.apiDesc')}</td>
                  <td><span className="badge badge-inactive">{t('integrations.planned')}</span></td>
                </tr>
              </tbody>
            </table>
          </div>

          <div className="settings-panel" style={{ marginTop: '16px' }}>
            <h3 style={{ color: '#fff', margin: '0 0 8px 0', fontSize: '14px' }}>{t('integrations.roadmap')}</h3>
            <p className="settings-hint" style={{ margin: 0, lineHeight: '1.6' }}>
              {t('integrations.roadmapDesc')}
            </p>
          </div>
        </div>
      )}

      {/* Memories Tab */}
      {activeTab === 'memories' && (
        <MemoriesTab error={error} success={success} setError={setError} setSuccess={setSuccess} clearMessages={clearMessages} />
      )}

      {/* Guardrails Tab */}
      {activeTab === 'guardrails' && (() => {
        const cfg = getConfig();
        const arnParts = cfg.agentRuntimeArn.split(':');
        const runtimeId = arnParts.length >= 6 ? arnParts[5].replace('runtime/', '') : '';
        const agentName = runtimeId.replace(/-[^-]+$/, '');
        const resourceId = encodeURIComponent(
          `${cfg.agentRuntimeArn}/runtime-endpoint/DEFAULT:DEFAULT`
        );
        const evaluatorUrl = runtimeId
          ? `https://${cfg.region}.console.aws.amazon.com/cloudwatch/home?region=${cfg.region}`
            + `#/gen-ai-observability/agent-core/agent-alias/${runtimeId}/endpoint/DEFAULT/agent/${agentName}`
            + `?resourceId=${resourceId}&serviceName=${agentName}.DEFAULT&tabId=evaluations`
          : 'https://console.aws.amazon.com/cloudwatch/home#/gen-ai-observability';
        return (
        <div className="guardrails-section">
          <div className="settings-panel" style={{ marginBottom: '16px' }}>
            <h3 style={{ color: '#fff', margin: '0 0 12px 0', fontSize: '16px' }}>{t('guardrails.title')}</h3>
            <p className="settings-hint" style={{ margin: '0 0 16px 0' }}>
              {t('guardrails.desc')}
            </p>
          </div>

          <div className="skills-table-container">
            <table className="skills-table">
              <thead>
                <tr>
                  <th>{t('guardrails.colGuardrail')}</th>
                  <th>{t('guardrails.colDescription')}</th>
                  <th>{t('guardrails.colAction')}</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="cell-name">{t('guardrails.evaluator')}</td>
                  <td className="cell-desc">
                    {t('guardrails.evaluatorDesc')}
                  </td>
                  <td className="cell-actions">
                    <a
                      className="btn btn-sm btn-primary"
                      href={evaluatorUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {t('guardrails.openConsole')}
                    </a>
                  </td>
                </tr>
                <tr>
                  <td className="cell-name">{t('guardrails.bedrockGuardrails')}</td>
                  <td className="cell-desc">
                    {t('guardrails.bedrockDesc')}
                  </td>
                  <td className="cell-actions">
                    <a
                      className="btn btn-sm btn-primary"
                      href="https://console.aws.amazon.com/bedrock/home#/guardrails"
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {t('guardrails.openConsole')}
                    </a>
                  </td>
                </tr>
                <tr>
                  <td className="cell-name">{t('guardrails.cedarPolicy')}</td>
                  <td className="cell-desc">
                    {t('guardrails.cedarDesc')}
                  </td>
                  <td className="cell-actions">
                    <button className="btn btn-sm btn-secondary" onClick={() => setActiveTab('users')}>
                      {t('guardrails.goToToolAccess')}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        );
      })()}
    </div>
  );
};

export default AdminConsole;
