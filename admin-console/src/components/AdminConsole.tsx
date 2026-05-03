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
  listRegistryRecords,
  importRegistryRecords,
  listA2aAgents,
  A2AAgentRecord,
  getAgentPrompts,
  saveAgentPrompt,
  deleteAgentPrompt,
  RegistryRecord,
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
  AgentPromptsResponse,
  AgentType,
  PromptRecord,
} from '../api/adminApi';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import CloudscapeBox from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import FormField from '@cloudscape-design/components/form-field';
import CloudscapeHeader from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Table from '@cloudscape-design/components/table';
import Textarea from '@cloudscape-design/components/textarea';
import Modal from '@cloudscape-design/components/modal';
import { getConfig } from '../config';
import { useI18n } from '../i18n';
import { sanitizeActorId } from '../api/sanitizeActor';
import ShellModal, { ShellTarget } from './ShellModal';

export type ActiveTab =
  | 'overview'
  | 'integrations'
  | 'models'
  | 'skills'
  | 'agentPrompts'
  | 'users'
  | 'memories'
  | 'identity'
  | 'instanceType'
  | 'sessions'
  | 'guardrails'
  | 'observability'
  | 'evaluations'
  | 'knowledgeBase';

interface ActorRow {
  actorId: string;
  email: string | null;
}

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

// Multimodal (vision-capable) Bedrock models offered to the vision agent.
// Intentionally a narrower subset of AVAILABLE_MODELS — only models that
// accept image inputs in Bedrock Converse. Admins pick any one per user;
// empty string = use VISION_MODEL_ID env default (Claude Haiku 4.5).
const VISION_MODELS = [
  { id: '', label: '── Claude (multimodal) ──', disabled: true },
  { id: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', label: 'Claude Haiku 4.5' },
  { id: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0', label: 'Claude Sonnet 4.5' },
  { id: 'us.anthropic.claude-sonnet-4-6', label: 'Claude Sonnet 4.6' },
  { id: 'us.anthropic.claude-opus-4-5-20251101-v1:0', label: 'Claude Opus 4.5' },
  { id: 'us.anthropic.claude-opus-4-6-v1', label: 'Claude Opus 4.6' },
  { id: 'us.anthropic.claude-3-7-sonnet-20250219-v1:0', label: 'Claude 3.7 Sonnet' },
  { id: 'us.anthropic.claude-3-5-haiku-20241022-v1:0', label: 'Claude 3.5 Haiku' },
  { id: '', label: '── Nova ──', disabled: true },
  { id: 'us.amazon.nova-pro-v1:0', label: 'Nova Pro' },
  { id: 'us.amazon.nova-lite-v1:0', label: 'Nova Lite' },
  { id: '', label: '── Qwen (multimodal) ──', disabled: true },
  { id: 'qwen.qwen3-vl-235b-a22b', label: 'Qwen3 VL 235B A22B' },
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
  const [globalVisionModelId, setGlobalVisionModelId] = useState('');
  const [savedGlobalVisionModelId, setSavedGlobalVisionModelId] = useState('');
  const [users, setUsers] = useState<CognitoUserInfo[]>([]);
  const [userModels, setUserModels] = useState<Record<string, string>>({});
  const [savedUserModels, setSavedUserModels] = useState<Record<string, string>>({});
  const [userVisionModels, setUserVisionModels] = useState<Record<string, string>>({});
  const [savedUserVisionModels, setSavedUserVisionModels] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const { t } = useI18n();

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const globalSettings = await getSettings('__global__');
      setGlobalModelId(globalSettings.modelId || '');
      setSavedGlobalModelId(globalSettings.modelId || '');
      setGlobalVisionModelId(globalSettings.visionModelId || '');
      setSavedGlobalVisionModelId(globalSettings.visionModelId || '');

      const cognitoUsers = await listCognitoUsers();
      setUsers(cognitoUsers);

      const models: Record<string, string> = {};
      const visionModels: Record<string, string> = {};
      for (const u of cognitoUsers) {
        try {
          const s = await getSettings(u.email || u.username || u.sub);
          models[u.sub] = s.modelId || '';
          visionModels[u.sub] = s.visionModelId || '';
        } catch {
          models[u.sub] = '';
          visionModels[u.sub] = '';
        }
      }
      setUserModels(models);
      setSavedUserModels({ ...models });
      setUserVisionModels(visionModels);
      setSavedUserVisionModels({ ...visionModels });
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
      await updateSettings('__global__', {
        modelId: globalModelId,
        visionModelId: globalVisionModelId,
      });
      setSavedGlobalModelId(globalModelId);
      setSavedGlobalVisionModelId(globalVisionModelId);
      setSuccess(t('models.globalUpdated'));
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleSaveUserModel = async (user: CognitoUserInfo) => {
    clearMessages();
    const userId = user.email || user.username || user.sub;
    const newModel = userModels[user.sub] || '';
    const newVisionModel = userVisionModels[user.sub] || '';
    try {
      await updateSettings(userId, { modelId: newModel, visionModelId: newVisionModel });
      setSavedUserModels((prev) => ({ ...prev, [user.sub]: newModel }));
      setSavedUserVisionModels((prev) => ({ ...prev, [user.sub]: newVisionModel }));
      setSuccess(t('models.userUpdated').replace('{user}', user.email || user.username || ''));
    } catch (err: any) {
      setError(err.message);
    }
  };

  const modelOptions = [
    { value: '', label: t('models.notSet') },
    ...AVAILABLE_MODELS.map((m, i) =>
      (m as any).disabled
        ? { value: `__group__${i}`, label: m.label, disabled: true }
        : { value: (m as any).id as string, label: m.label }
    ),
  ];
  const userModelOptions = [
    { value: '', label: t('models.useGlobalDefault') },
    ...AVAILABLE_MODELS.map((m, i) =>
      (m as any).disabled
        ? { value: `__group__${i}`, label: m.label, disabled: true }
        : { value: (m as any).id as string, label: m.label }
    ),
  ];
  const visionModelOptions = [
    { value: '', label: t('models.notSet') },
    ...VISION_MODELS.map((m, i) =>
      (m as any).disabled
        ? { value: `__vgroup__${i}`, label: m.label, disabled: true }
        : { value: (m as any).id as string, label: m.label }
    ),
  ];
  const userVisionModelOptions = [
    { value: '', label: t('models.useGlobalDefault') },
    ...VISION_MODELS.map((m, i) =>
      (m as any).disabled
        ? { value: `__vgroup__${i}`, label: m.label, disabled: true }
        : { value: (m as any).id as string, label: m.label }
    ),
  ];
  const findOption = (opts: typeof modelOptions, value: string) =>
    opts.find((o) => o.value === value) ?? opts[0];

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
      {success && <Alert type="success">{success}</Alert>}

      <Container
        header={
          <CloudscapeHeader variant="h2" description={t('models.globalHint')}>
            {t('models.globalDefault')}
          </CloudscapeHeader>
        }
      >
        <SpaceBetween size="s">
          <FormField label={t('models.textModelLabel')}>
            <div style={{ minWidth: 320 }}>
              <Select
                selectedOption={findOption(modelOptions, globalModelId)}
                onChange={({ detail }) => setGlobalModelId((detail.selectedOption.value as string) || '')}
                options={modelOptions}
              />
            </div>
          </FormField>
          <FormField label={t('models.visionModelLabel')} description={t('models.visionModelHint')}>
            <div style={{ minWidth: 320 }}>
              <Select
                selectedOption={findOption(visionModelOptions, globalVisionModelId)}
                onChange={({ detail }) => setGlobalVisionModelId((detail.selectedOption.value as string) || '')}
                options={visionModelOptions}
              />
            </div>
          </FormField>
          <Button
            variant="primary"
            onClick={handleSaveGlobal}
            disabled={
              globalModelId === savedGlobalModelId
              && globalVisionModelId === savedGlobalVisionModelId
            }
          >
            {t('models.save')}
          </Button>
        </SpaceBetween>
      </Container>

      <Table
        header={
          <CloudscapeHeader
            variant="h2"
            actions={
              <Button iconName="refresh" onClick={loadData}>
                {t('models.refresh')}
              </Button>
            }
          >
            {t('models.perUser')}
          </CloudscapeHeader>
        }
        loading={loading}
        loadingText={t('models.loadingUsers')}
        items={users}
        trackBy="sub"
        columnDefinitions={[
          { id: 'email', header: t('models.colEmail'), cell: (u) => u.email || u.username },
          {
            id: 'status',
            header: t('models.colStatus'),
            cell: (u) =>
              u.status === 'CONFIRMED' ? (
                <StatusIndicator type="success">{u.status}</StatusIndicator>
              ) : (
                <StatusIndicator type="stopped">{u.status}</StatusIndicator>
              ),
          },
          {
            id: 'model',
            header: t('models.colModel'),
            cell: (u) => (
              <div style={{ minWidth: 260 }}>
                <Select
                  selectedOption={findOption(userModelOptions, userModels[u.sub] || '')}
                  onChange={({ detail }) =>
                    setUserModels((prev) => ({ ...prev, [u.sub]: (detail.selectedOption.value as string) || '' }))
                  }
                  options={userModelOptions}
                />
              </div>
            ),
          },
          {
            id: 'visionModel',
            header: t('models.colVisionModel'),
            cell: (u) => (
              <div style={{ minWidth: 240 }}>
                <Select
                  selectedOption={findOption(userVisionModelOptions, userVisionModels[u.sub] || '')}
                  onChange={({ detail }) =>
                    setUserVisionModels((prev) => ({ ...prev, [u.sub]: (detail.selectedOption.value as string) || '' }))
                  }
                  options={userVisionModelOptions}
                />
              </div>
            ),
          },
          {
            id: 'actions',
            header: t('models.colActions'),
            minWidth: 120,
            cell: (u) => (
              <Button
                variant="primary"
                onClick={() => handleSaveUserModel(u)}
                disabled={
                  (userModels[u.sub] || '') === (savedUserModels[u.sub] || '')
                  && (userVisionModels[u.sub] || '') === (savedUserVisionModels[u.sub] || '')
                }
              >
                {t('models.save')}
              </Button>
            ),
          },
        ]}
        empty={
          <CloudscapeBox textAlign="center" padding="m">
            <b>{t('models.noUsers')}</b>
          </CloudscapeBox>
        }
      />
    </SpaceBetween>
  );
};

// ---------------------------------------------------------------------------
// Agent Prompt Tab — standalone component
// ---------------------------------------------------------------------------
interface AgentPromptTabProps {
  error: string;
  success: string;
  clearMessages: () => void;
  setError: (msg: string) => void;
  setSuccess: (msg: string) => void;
  cognitoUsers: CognitoUserInfo[];
}

interface PromptEditorCardProps {
  agentType: AgentType;
  title: string;
  hint: string;
  scope: string;
  record: PromptRecord;
  draft: string;
  onChangeDraft: (value: string) => void;
  onSave: () => void;
  onDiscard: () => void;
  onReset: () => void;
  saving: boolean;
}

const PROMPT_TEXTAREA_STYLE: React.CSSProperties = {
  width: '100%',
  fontFamily: 'ui-monospace, Menlo, Consolas, monospace',
  fontSize: '13px',
  lineHeight: '1.5',
  padding: '12px',
  background: '#151515',
  color: '#eaeaea',
  border: '1px solid #2a2a2a',
  borderRadius: '4px',
  resize: 'vertical',
  boxSizing: 'border-box',
};

const PromptEditorCard: React.FC<PromptEditorCardProps> = ({
  agentType,
  title,
  hint,
  scope,
  record,
  draft,
  onChangeDraft,
  onSave,
  onDiscard,
  onReset,
  saving,
}) => {
  const { t } = useI18n();
  const isGlobalScope = scope === '__global__';
  // "Dirty" = draft differs from the effective *starting* state admins were
  // shown. At global scope with no override that starting state is the
  // built-in default (so typing default verbatim shouldn't count as dirty);
  // otherwise it's the saved body.
  const startingState = isGlobalScope && !record.isOverride
    ? record.builtinDefault
    : record.body;
  const dirty = draft !== startingState;

  // Effective prompt the agent will see: global_body + "\n\n" + user_body
  // (any empty part omitted). At Global scope, `draft` is the editable global
  // body. At User scope, `draft` is only the user addendum — the read-only
  // global context is shown above it.
  const effectiveGlobal = isGlobalScope ? draft.trim() : record.globalBody.trim();
  const effectiveUser = isGlobalScope ? '' : draft.trim();
  const effectiveParts = [effectiveGlobal, effectiveUser].filter(Boolean);
  const effectivePrompt = effectiveParts.length
    ? effectiveParts.join('\n\n')
    : record.builtinDefault;

  const badge = isGlobalScope
    ? record.isOverride
      ? <StatusIndicator type="success">{t('prompts.badgeGlobalCustom')}</StatusIndicator>
      : <StatusIndicator type="stopped">{t('prompts.badgeGlobalDefault')}</StatusIndicator>
    : record.isOverride
      ? <StatusIndicator type="success">{t('prompts.badgeUserSet')}</StatusIndicator>
      : <StatusIndicator type="stopped">{t('prompts.badgeUserEmpty')}</StatusIndicator>;

  const editorRows = isGlobalScope
    ? agentType === 'voice' ? 12 : 18
    : agentType === 'voice' ? 8 : 10;

  return (
    <div style={{ flex: '1 1 0', minWidth: '320px' }}>
      <Container
        header={
          <CloudscapeHeader
            variant="h3"
            description={hint}
            actions={badge}
          >
            {title}
          </CloudscapeHeader>
        }
      >
        <SpaceBetween size="m">
          {!isGlobalScope && (
            <FormField
              label={t('prompts.globalBaseLabel')}
              description={t('prompts.globalBaseHint')}
            >
              <Textarea
                value={record.globalBody || t('prompts.globalBaseEmpty')}
                readOnly
                spellcheck={false}
                rows={6}
              />
            </FormField>
          )}
          <FormField
            label={!isGlobalScope ? t('prompts.userAddendumLabel') : undefined}
            description={!isGlobalScope ? t('prompts.userAddendumHint') : undefined}
          >
            <Textarea
              value={draft}
              onChange={({ detail }) => onChangeDraft(detail.value)}
              spellcheck={false}
              rows={editorRows}
            />
          </FormField>

          <SpaceBetween direction="horizontal" size="xs">
            <Button
              variant="primary"
              onClick={onSave}
              disabled={!dirty || saving || !draft.trim()}
              loading={saving}
            >
              {t('prompts.save')}
            </Button>
            <Button onClick={onDiscard} disabled={!dirty}>
              {t('prompts.revert')}
            </Button>
            {record.isOverride && (
              <Button onClick={onReset} disabled={saving}>
                {isGlobalScope ? t('prompts.revertToDefault') : t('prompts.removeOverride')}
              </Button>
            )}
          </SpaceBetween>

          {record.updatedAt && record.isOverride && (
            <CloudscapeBox color="text-body-secondary" fontSize="body-s">
              {t('prompts.lastEdited')} {new Date(record.updatedAt).toLocaleString()}
              {record.updatedBy ? ` · ${t('prompts.lastEditedBy')} ${record.updatedBy}` : ''}
            </CloudscapeBox>
          )}

          {!isGlobalScope && (
            <details>
              <summary style={{ cursor: 'pointer', fontWeight: 600, fontSize: 13 }}>
                {t('prompts.effectivePreview')}
              </summary>
              <pre
                style={{
                  ...PROMPT_TEXTAREA_STYLE,
                  minHeight: '140px',
                  maxHeight: '300px',
                  overflow: 'auto',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  marginTop: '6px',
                }}
              >
                {effectivePrompt}
              </pre>
            </details>
          )}

          <Alert type="info" header={t('prompts.evoCardTitle')}>
            {t('prompts.evoCardComingSoon')}
          </Alert>
        </SpaceBetween>
      </Container>
    </div>
  );
};

const AgentPromptTab: React.FC<AgentPromptTabProps> = ({
  error,
  success,
  clearMessages,
  setError,
  setSuccess,
  cognitoUsers,
}) => {
  const { t } = useI18n();
  const [selectedScope, setSelectedScope] = useState<string>('__global__');
  const [prompts, setPrompts] = useState<AgentPromptsResponse | null>(null);
  const [textDraft, setTextDraft] = useState('');
  const [voiceDraft, setVoiceDraft] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState<AgentType | null>(null);

  const scopeLabel = useCallback((scope: string): string => {
    if (scope === '__global__') return t('prompts.globalScope');
    const match = cognitoUsers.find(
      (u) => u.email === scope || u.username === scope || u.sub === scope
    );
    return match?.email || match?.username || scope;
  }, [cognitoUsers, t]);

  const load = useCallback(async (scope: string) => {
    setLoading(true);
    try {
      const data = await getAgentPrompts(scope);
      setPrompts(data);
      // At Global scope, when no override exists yet, seed the editor with
      // the built-in default so admins have a starting point rather than an
      // empty textarea. At user scope, draft = the user's addendum ("" is
      // the expected starting state for a fresh user).
      const seed = (r: PromptRecord) =>
        scope === '__global__'
          ? (r.isOverride ? r.body : r.builtinDefault)
          : r.body;
      setTextDraft(seed(data.text));
      setVoiceDraft(seed(data.voice));
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [setError]);

  useEffect(() => { load(selectedScope); }, [load, selectedScope]);

  const agentLabel = (agentType: AgentType) =>
    agentType === 'text' ? t('prompts.textAgent') : t('prompts.voiceAgent');

  const handleSave = async (agentType: AgentType) => {
    clearMessages();
    const draft = agentType === 'text' ? textDraft : voiceDraft;
    if (!draft.trim()) return;
    setSaving(agentType);
    try {
      await saveAgentPrompt(selectedScope, agentType, draft);
      setSuccess(
        t('prompts.saveSuccess')
          .replace('{agent}', agentLabel(agentType))
          .replace('{scope}', scopeLabel(selectedScope))
      );
      await load(selectedScope);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSaving(null);
    }
  };

  const handleRevertDraft = (agentType: AgentType) => {
    if (!prompts) return;
    const r = agentType === 'text' ? prompts.text : prompts.voice;
    const seed = selectedScope === '__global__'
      ? (r.isOverride ? r.body : r.builtinDefault)
      : r.body;
    if (agentType === 'text') setTextDraft(seed);
    else setVoiceDraft(seed);
  };

  const handleReset = async (agentType: AgentType) => {
    // At Global scope this reverts to the built-in default;
    // at user scope it removes the user's addendum (Global still applies).
    const confirmMsg = selectedScope === '__global__'
      ? t('prompts.confirmRevertGlobal')
      : t('prompts.confirmRemoveUser');
    if (!window.confirm(confirmMsg)) return;
    clearMessages();
    setSaving(agentType);
    try {
      await deleteAgentPrompt(selectedScope, agentType);
      setSuccess(
        t('prompts.revertSuccess')
          .replace('{agent}', agentLabel(agentType))
          .replace('{scope}', scopeLabel(selectedScope))
      );
      await load(selectedScope);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSaving(null);
    }
  };

  const scopeOptions: string[] = [
    '__global__',
    ...cognitoUsers
      .map((u) => u.email || u.username)
      .filter((s): s is string => !!s),
  ];

  const scopeSelectOptions = scopeOptions.map((scope) => ({
    value: scope,
    label: scopeLabel(scope),
  }));
  const selectedScopeOption =
    scopeSelectOptions.find((o) => o.value === selectedScope) ?? scopeSelectOptions[0];

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
      {success && <Alert type="success">{success}</Alert>}

      <Container
        header={
          <CloudscapeHeader variant="h2" description={t('prompts.desc')}>
            {t('prompts.title')}
          </CloudscapeHeader>
        }
      >
        <FormField label={t('prompts.userScope')}>
          <div style={{ maxWidth: 360 }}>
            <Select
              selectedOption={selectedScopeOption}
              onChange={({ detail }) => setSelectedScope(detail.selectedOption.value as string)}
              options={scopeSelectOptions}
            />
          </div>
        </FormField>
      </Container>

      {loading || !prompts ? (
        <CloudscapeBox textAlign="center" padding="l">
          <StatusIndicator type="loading">{t('prompts.loading')}</StatusIndicator>
        </CloudscapeBox>
      ) : (
        <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
          <PromptEditorCard
            agentType="text"
            title={t('prompts.textAgent')}
            hint={t('prompts.textAgentHint')}
            scope={selectedScope}
            record={prompts.text}
            draft={textDraft}
            onChangeDraft={setTextDraft}
            onSave={() => handleSave('text')}
            onDiscard={() => handleRevertDraft('text')}
            onReset={() => handleReset('text')}
            saving={saving === 'text'}
          />
          <PromptEditorCard
            agentType="voice"
            title={t('prompts.voiceAgent')}
            hint={t('prompts.voiceAgentHint')}
            scope={selectedScope}
            record={prompts.voice}
            draft={voiceDraft}
            onChangeDraft={setVoiceDraft}
            onSave={() => handleSave('voice')}
            onDiscard={() => handleRevertDraft('voice')}
            onReset={() => handleReset('voice')}
            saving={saving === 'voice'}
          />
        </div>
      )}
    </SpaceBetween>
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
  const [actors, setActors] = useState<ActorRow[]>([]);
  const [selectedActor, setSelectedActor] = useState<ActorRow | null>(null);
  const [records, setRecords] = useState<MemoryRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [recordsLoading, setRecordsLoading] = useState(false);
  const { t } = useI18n();

  const loadActors = useCallback(async () => {
    setLoading(true);
    try {
      // Cognito lookup is best-effort — if it fails the table still shows
      // raw actorIds instead of blocking the whole tab.
      const [a, users] = await Promise.all([
        listMemoryActors(),
        listCognitoUsers().catch((err) => {
          console.warn('listCognitoUsers failed; actorIds will not be resolved to emails', err);
          return [] as CognitoUserInfo[];
        }),
      ]);
      const emailByActor = new Map<string, string>();
      for (const u of users) {
        if (u.email) {
          emailByActor.set(sanitizeActorId(u.email), u.email);
          emailByActor.set(sanitizeActorId(u.sub), u.email);
        }
      }
      const rows: ActorRow[] = a.map((actorId) => ({
        actorId,
        email: emailByActor.get(actorId) ?? null,
      }));
      rows.sort((x, y) => {
        if (x.email && !y.email) return -1;
        if (!x.email && y.email) return 1;
        return (x.email ?? x.actorId).localeCompare(y.email ?? y.actorId);
      });
      setActors(rows);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [setError]);

  useEffect(() => { loadActors(); }, [loadActors]);

  const handleSelectActor = async (row: ActorRow) => {
    clearMessages();
    setSelectedActor(row);
    setRecordsLoading(true);
    try {
      const r = await getMemoryRecords(row.actorId);
      setRecords(r);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setRecordsLoading(false);
    }
  };

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
      {success && <Alert type="success">{success}</Alert>}

      {!selectedActor && (
        <Table
          header={
            <CloudscapeHeader
              variant="h2"
              actions={
                <Button iconName="refresh" onClick={() => { setSelectedActor(null); loadActors(); }}>
                  {t('memories.refresh')}
                </Button>
              }
            >
              {t('memories.title')}
            </CloudscapeHeader>
          }
          loading={loading}
          loadingText={t('memories.loadingActors')}
          items={actors}
          trackBy="actorId"
          columnDefinitions={[
            { id: 'email', header: t('memories.colEmail'), cell: (row) => row.email ?? '—', sortingField: 'email' },
            { id: 'actorId', header: t('memories.colActorId'), cell: (row) => <code>{row.actorId}</code> },
            {
              id: 'actions',
              header: t('memories.colActions'),
              minWidth: 160,
              cell: (row) => (
                <Button variant="inline-link" onClick={() => handleSelectActor(row)}>
                  {t('memories.viewMemories')}
                </Button>
              ),
            },
          ]}
          empty={
            <CloudscapeBox textAlign="center" padding="m">
              <b>{t('memories.noActors')}</b>
              <CloudscapeBox variant="p" color="text-body-secondary" padding={{ top: 'xs' }}>
                {t('memories.noActorsHint')}
              </CloudscapeBox>
            </CloudscapeBox>
          }
        />
      )}

      {selectedActor && (
        <Table
          header={
            <CloudscapeHeader
              variant="h2"
              description={
                selectedActor.email ? (
                  <>
                    {selectedActor.email} <code>({selectedActor.actorId})</code>
                  </>
                ) : (
                  <code>{selectedActor.actorId}</code>
                )
              }
              actions={
                <Button onClick={() => setSelectedActor(null)}>
                  {t('memories.backToActors')}
                </Button>
              }
            >
              {t('memories.memoriesFor')}
            </CloudscapeHeader>
          }
          loading={recordsLoading}
          loadingText={t('memories.loadingMemories')}
          items={records}
          trackBy="id"
          columnDefinitions={[
            {
              id: 'type',
              header: t('memories.colType'),
              cell: (r) =>
                r.type === 'facts' ? (
                  <StatusIndicator type="success">{r.type}</StatusIndicator>
                ) : (
                  <Badge color="blue">{r.type}</Badge>
                ),
            },
            {
              id: 'content',
              header: t('memories.colContent'),
              cell: (r) => <span style={{ whiteSpace: 'normal' }}>{r.text}</span>,
              maxWidth: 600,
            },
            {
              id: 'created',
              header: t('memories.colCreated'),
              cell: (r) => (r.createdAt ? new Date(r.createdAt).toLocaleString() : '-'),
            },
          ]}
          empty={
            <CloudscapeBox textAlign="center" padding="m">
              <b>{t('memories.noRecords')}</b>
            </CloudscapeBox>
          }
        />
      )}
    </SpaceBetween>
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

  const scopeSelectOptions = scopeItems.map((s) => ({ value: s.scope, label: displayScope(s.scope) }));
  const selectedScopeOption =
    scopeSelectOptions.find((o) => o.value === selectedScope) ?? scopeSelectOptions[0];

  const kbStatusIndicator =
    kbStatus === 'ACTIVE' ? <StatusIndicator type="success">{t('kb.statusActive')}</StatusIndicator>
    : kbStatus === 'NOT_INITIALIZED' ? <StatusIndicator type="stopped">{t('kb.statusNotInit')}</StatusIndicator>
    : <StatusIndicator type="pending">{kbStatus}</StatusIndicator>;

  const fileInputId = 'kb-file-input';

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
      {success && <Alert type="success">{success}</Alert>}

      <Container
        header={
          <CloudscapeHeader variant="h2" description={t('kb.desc')} actions={kbStatusIndicator}>
            {t('kb.title')}
          </CloudscapeHeader>
        }
      >
        <SpaceBetween size="s">
          <CloudscapeBox fontSize="body-s" color="text-body-secondary">
            {t('kb.scopeSummary')}
          </CloudscapeBox>
          <SpaceBetween direction="horizontal" size="xs">
            {scopeItems.map((s) => (
              <Button
                key={s.scope}
                variant={selectedScope === s.scope ? 'primary' : 'normal'}
                onClick={() => setSelectedScope(s.scope)}
              >
                {displayScope(s.scope)} ({s.documentCount})
              </Button>
            ))}
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      <Table
        header={
          <CloudscapeHeader
            variant="h2"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <input
                  type="file"
                  id={fileInputId}
                  style={{ display: 'none' }}
                  disabled={uploading}
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) {
                      handleUpload(file);
                      e.target.value = '';
                    }
                  }}
                />
                <Button
                  variant="primary"
                  loading={uploading}
                  onClick={() => document.getElementById(fileInputId)?.click()}
                >
                  {t('kb.uploadDoc')}
                </Button>
                <Button onClick={handleSync} loading={syncing}>
                  {t('kb.sync')}
                </Button>
                <Button iconName="refresh" onClick={() => { loadStatus(); loadDocuments(selectedScope); loadSyncStatus(); }}>
                  {t('kb.refresh')}
                </Button>
              </SpaceBetween>
            }
          >
            <SpaceBetween direction="horizontal" size="xs" alignItems="center">
              <span>{t('kb.scope')}</span>
              <div style={{ minWidth: 220 }}>
                <Select
                  selectedOption={selectedScopeOption}
                  onChange={({ detail }) => setSelectedScope(detail.selectedOption.value as string)}
                  options={scopeSelectOptions}
                />
              </div>
              <Button onClick={handleAddScope}>{t('kb.addScope')}</Button>
            </SpaceBetween>
          </CloudscapeHeader>
        }
        loading={docsLoading}
        loadingText={t('files.loading')}
        items={documents}
        trackBy="key"
        columnDefinitions={[
          { id: 'name', header: t('kb.colName'), cell: (doc) => doc.name },
          {
            id: 'size',
            header: t('kb.colSize'),
            cell: (doc) =>
              doc.size < 1024
                ? `${doc.size} B`
                : doc.size < 1048576
                  ? `${(doc.size / 1024).toFixed(1)} KB`
                  : `${(doc.size / 1048576).toFixed(1)} MB`,
          },
          {
            id: 'modified',
            header: t('kb.colModified'),
            cell: (doc) => new Date(doc.lastModified).toLocaleDateString(),
          },
          {
            id: 'actions',
            header: t('kb.colActions'),
            minWidth: 110,
            cell: (doc) => <Button onClick={() => handleDelete(doc.key)}>{t('kb.delete')}</Button>,
          },
        ]}
        empty={
          <CloudscapeBox textAlign="center" padding="m">
            <b>{kbStatus === 'NOT_INITIALIZED' ? t('kb.notInitialized') : t('kb.noDocuments')}</b>
            <CloudscapeBox variant="p" color="text-body-secondary" padding={{ top: 'xs' }}>
              {t('kb.noDocumentsHint')}
            </CloudscapeBox>
          </CloudscapeBox>
        }
      />

      {syncJobs.length > 0 && (
        <Table
          header={<CloudscapeHeader variant="h3">{t('kb.syncStatus')}</CloudscapeHeader>}
          items={syncJobs}
          trackBy="ingestionJobId"
          columnDefinitions={[
            {
              id: 'status',
              header: t('kb.syncJobStatus'),
              cell: (job) =>
                job.status === 'COMPLETE' ? (
                  <StatusIndicator type="success">{job.status}</StatusIndicator>
                ) : job.status === 'IN_PROGRESS' || job.status === 'STARTING' ? (
                  <StatusIndicator type="in-progress">{job.status}</StatusIndicator>
                ) : (
                  <StatusIndicator type="stopped">{job.status}</StatusIndicator>
                ),
            },
            {
              id: 'started',
              header: t('kb.syncJobStarted'),
              cell: (job) => (job.startedAt ? new Date(job.startedAt).toLocaleString() : '-'),
            },
            {
              id: 'updated',
              header: t('kb.syncJobUpdated'),
              cell: (job) => (job.updatedAt ? new Date(job.updatedAt).toLocaleString() : '-'),
            },
          ]}
        />
      )}
    </SpaceBetween>
  );
};

// ---------------------------------------------------------------------------
// Main AdminConsole component
// ---------------------------------------------------------------------------
interface AdminConsoleProps {
  activeTab: ActiveTab;
  setActiveTab: (t: ActiveTab) => void;
}
const AdminConsole: React.FC<AdminConsoleProps> = ({ activeTab, setActiveTab }) => {
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

  // Registry import modal state
  const [showRegistryModal, setShowRegistryModal] = useState(false);
  const [registryRecords, setRegistryRecords] = useState<RegistryRecord[]>([]);
  const [registryLoading, setRegistryLoading] = useState(false);
  const [registrySelections, setRegistrySelections] = useState<Record<string, boolean>>({});
  const [registryTargetUser, setRegistryTargetUser] = useState<string>('__global__');
  const [registryImporting, setRegistryImporting] = useState(false);

  // User settings (model ID)
  const [modelId, setModelId] = useState('');
  const [savedModelId, setSavedModelId] = useState('');

  // Active tab is hoisted to App.tsx so the SideNavigation can drive it.

  // Sessions
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [shellTarget, setShellTarget] = useState<ShellTarget | null>(null);

  // Integration Registry
  const [integrationsSubTab, setIntegrationsSubTab] = useState<'overview' | 'a2a'>('overview');
  const [a2aAgents, setA2aAgents] = useState<A2AAgentRecord[]>([]);
  const [a2aLoading, setA2aLoading] = useState(false);
  const [a2aError, setA2aError] = useState<string>('');
  const [a2aDrawer, setA2aDrawer] = useState<A2AAgentRecord | null>(null);

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

  const handleStopSession = async (sessionId: string, kind?: 'text' | 'voice') => {
    clearMessages();
    try {
      await stopSession(sessionId, kind);
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
      // If the user has no explicit permission record yet (empty list),
      // default-allow every built-in tool per spec. Gateway-scanned tools
      // stay unchecked by default — admins opt users in explicitly.
      const initialAllowed = allowed.length === 0
        ? gatewayTools.filter((t) => t.source === 'builtin').map((t) => t.name)
        : allowed;
      setPermOriginal(initialAllowed);
      const selections: Record<string, boolean> = {};
      for (const tool of gatewayTools) {
        selections[tool.name] = initialAllowed.includes(tool.name);
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

  useEffect(() => {
    if (activeTab === 'integrations' && integrationsSubTab === 'a2a') {
      setA2aLoading(true);
      setA2aError('');
      listA2aAgents()
        .then(setA2aAgents)
        .catch((err: any) => setA2aError(err.message))
        .finally(() => setA2aLoading(false));
    }
  }, [activeTab, integrationsSubTab]);

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

  const handleOpenRegistryModal = async () => {
    clearMessages();
    setShowRegistryModal(true);
    setRegistryLoading(true);
    setRegistrySelections({});
    setRegistryTargetUser(selectedUserId);
    try {
      const records = await listRegistryRecords('APPROVED');
      setRegistryRecords(records);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setRegistryLoading(false);
    }
  };

  const handleRegistryImport = async () => {
    clearMessages();
    const selectedIds = Object.entries(registrySelections)
      .filter(([, v]) => v)
      .map(([k]) => k);
    if (selectedIds.length === 0) {
      setError(t('registry.selectAtLeastOne'));
      return;
    }
    setRegistryImporting(true);
    try {
      const result = await importRegistryRecords(selectedIds, registryTargetUser);
      if (result.errors && result.errors.length > 0) {
        setError(result.errors.join('; '));
      }
      if (result.imported && result.imported.length > 0) {
        setSuccess(
          t('registry.importedMsg')
            .replace('{n}', String(result.imported.length))
            .replace('{user}', displayUserId(registryTargetUser))
        );
      }
      setShowRegistryModal(false);
      setRegistrySelections({});
      loadSkills();
      loadUserIds();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setRegistryImporting(false);
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
      {activeTab === 'overview' && (
        <SpaceBetween size="l">
          <Container
            header={
              <CloudscapeHeader variant="h1" description={t('overview.desc')}>
                {t('overview.title')}
              </CloudscapeHeader>
            }
          >
            <SpaceBetween size="m">
              <CloudscapeBox variant="p">{t('overview.intro')}</CloudscapeBox>
              <Alert type="info">{t('overview.diagramPlaceholder')}</Alert>
            </SpaceBetween>
          </Container>
        </SpaceBetween>
      )}

      {activeTab === 'identity' && (
        <Table
          header={
            <CloudscapeHeader variant="h2" description={t('identity.desc')}>
              {t('identity.title')}
            </CloudscapeHeader>
          }
          loading={usersLoading}
          loadingText={t('users.loadingUsers')}
          items={cognitoUsers}
          trackBy="sub"
          columnDefinitions={[
            { id: 'email', header: t('users.colEmail'), cell: (u) => u.email || u.username },
            {
              id: 'status',
              header: t('users.colStatus'),
              cell: (u) =>
                u.status === 'CONFIRMED' ? (
                  <StatusIndicator type="success">{u.status}</StatusIndicator>
                ) : (
                  <StatusIndicator type="stopped">{u.status}</StatusIndicator>
                ),
            },
            {
              id: 'groups',
              header: t('users.colGroups'),
              cell: (u) =>
                u.groups.length > 0 ? (
                  <SpaceBetween direction="horizontal" size="xxs">
                    {u.groups.map((g) => (
                      <Badge key={g} color={g === 'admin' ? 'red' : 'blue'}>{g}</Badge>
                    ))}
                  </SpaceBetween>
                ) : '-',
            },
            {
              id: 'created',
              header: t('identity.colCreated'),
              cell: (u) => (u.createdAt ? new Date(u.createdAt).toLocaleString() : '-'),
            },
            {
              id: 'userId',
              header: t('users.colUserId'),
              cell: (u) => <span title={u.sub}>{u.sub.length > 28 ? u.sub.slice(0, 28) + '...' : u.sub}</span>,
            },
          ]}
          empty={<CloudscapeBox textAlign="center" padding="m"><b>{t('users.noUsers')}</b></CloudscapeBox>}
        />
      )}

      {activeTab === 'instanceType' && (
        <Container
          header={
            <CloudscapeHeader variant="h2" description={t('instanceType.desc')}>
              {t('instanceType.title')}
            </CloudscapeHeader>
          }
        >
          <SpaceBetween size="m">
            <Alert type="info">{t('instanceType.comingSoon')}</Alert>
            <Table
              items={[
                { id: 'micro', name: 'MicroVM', status: 'default', description: t('instanceType.microDesc') },
                { id: 'ec2', name: 'EC2', status: 'planned', description: t('instanceType.ec2Desc') },
              ]}
              trackBy="id"
              columnDefinitions={[
                { id: 'name', header: t('instanceType.colName'), cell: (r) => r.name },
                { id: 'description', header: t('instanceType.colDescription'), cell: (r) => r.description },
                {
                  id: 'status',
                  header: t('instanceType.colStatus'),
                  cell: (r) =>
                    r.status === 'default' ? (
                      <StatusIndicator type="success">{t('instanceType.default')}</StatusIndicator>
                    ) : (
                      <StatusIndicator type="pending">{t('instanceType.planned')}</StatusIndicator>
                    ),
                },
              ]}
            />
          </SpaceBetween>
        </Container>
      )}

      {activeTab === 'observability' && (() => {
        const cfg = getConfig();
        const url = `https://${cfg.region}.console.aws.amazon.com/cloudwatch/home?region=${cfg.region}#/gen-ai-observability`;
        return (
          <Container
            header={
              <CloudscapeHeader variant="h2" description={t('observability.desc')}>
                {t('observability.title')}
              </CloudscapeHeader>
            }
          >
            <SpaceBetween size="m">
              <CloudscapeBox>{t('observability.intro')}</CloudscapeBox>
              <Button variant="primary" href={url} target="_blank" iconAlign="right" iconName="external">
                {t('observability.openConsole')}
              </Button>
            </SpaceBetween>
          </Container>
        );
      })()}

      {activeTab === 'evaluations' && (() => {
        const cfg = getConfig();
        const arnParts = cfg.agentRuntimeArn.split(':');
        const runtimeId = arnParts.length >= 6 ? arnParts[5].replace('runtime/', '') : '';
        const agentName = runtimeId.replace(/-[^-]+$/, '');
        const resourceId = encodeURIComponent(`${cfg.agentRuntimeArn}/runtime-endpoint/DEFAULT:DEFAULT`);
        const url = runtimeId
          ? `https://${cfg.region}.console.aws.amazon.com/cloudwatch/home?region=${cfg.region}#/gen-ai-observability/agent-core/agent-alias/${runtimeId}/endpoint/DEFAULT/agent/${agentName}?resourceId=${resourceId}&serviceName=${agentName}.DEFAULT&tabId=evaluations`
          : 'https://console.aws.amazon.com/cloudwatch/home#/gen-ai-observability';
        return (
          <Container
            header={
              <CloudscapeHeader variant="h2" description={t('evaluations.desc')}>
                {t('evaluations.title')}
              </CloudscapeHeader>
            }
          >
            <SpaceBetween size="m">
              <CloudscapeBox>{t('evaluations.intro')}</CloudscapeBox>
              <Button variant="primary" href={url} target="_blank" iconAlign="right" iconName="external">
                {t('evaluations.openConsole')}
              </Button>
            </SpaceBetween>
          </Container>
        );
      })()}

      {activeTab === 'skills' && (() => {
        const userScopeOptions = userIds.map((id) => ({ value: id, label: displayUserId(id) }));
        const selectedUserOption = userScopeOptions.find((o) => o.value === selectedUserId) ?? userScopeOptions[0];
        const registryTargetOption = userScopeOptions.find((o) => o.value === registryTargetUser) ?? userScopeOptions[0];
        return (
      <>
      {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
      {success && <Alert type="success">{success}</Alert>}

      <SpaceBetween size="s" direction="horizontal">
        <span style={{ alignSelf: 'center' }}>{t('skills.userScope')}</span>
        <div style={{ minWidth: 220 }}>
          <Select
            selectedOption={selectedUserOption}
            onChange={({ detail }) => setSelectedUserId(detail.selectedOption.value as string)}
            options={userScopeOptions}
          />
        </div>
        <Button onClick={handleAddUserScope}>{t('skills.addUser')}</Button>
        <div style={{ flex: 1 }} />
        <Button onClick={handleOpenRegistryModal}>{t('registry.addFromRegistry')}</Button>
        <Button variant="primary" onClick={handleCreate}>{t('skills.createSkill')}</Button>
      </SpaceBetween>

      {/* Registry import modal */}
      {showRegistryModal && (
        <Modal
          visible
          onDismiss={() => !registryImporting && setShowRegistryModal(false)}
          header={t('registry.modalTitle')}
          size="large"
          footer={
            <CloudscapeBox float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setShowRegistryModal(false)} disabled={registryImporting}>
                  {t('skills.cancel')}
                </Button>
                <Button
                  variant="primary"
                  onClick={handleRegistryImport}
                  loading={registryImporting}
                  disabled={registryLoading || registryRecords.length === 0}
                >
                  {t('registry.import')}
                </Button>
              </SpaceBetween>
            </CloudscapeBox>
          }
        >
          <SpaceBetween size="m">
            <p>{t('registry.modalHint')}</p>
            <FormField label={t('registry.targetScope')}>
              <Select
                selectedOption={registryTargetOption}
                onChange={({ detail }) => setRegistryTargetUser(detail.selectedOption.value as string)}
                options={userScopeOptions}
                disabled={registryImporting}
              />
            </FormField>
            <Table
              loading={registryLoading}
              loadingText={t('registry.loading')}
              items={registryRecords}
              trackBy="recordId"
              columnDefinitions={[
                {
                  id: 'select',
                  header: '',
                  cell: (r) => (
                    <input
                      type="checkbox"
                      checked={!!registrySelections[r.recordId]}
                      onChange={(e) =>
                        setRegistrySelections((prev) => ({ ...prev, [r.recordId]: e.target.checked }))
                      }
                      disabled={registryImporting}
                    />
                  ),
                },
                { id: 'name', header: t('registry.colName'), cell: (r) => r.name },
                { id: 'description', header: t('registry.colDescription'), cell: (r) => r.description },
                { id: 'version', header: t('registry.colVersion'), cell: (r) => r.recordVersion },
                {
                  id: 'updated',
                  header: t('registry.colUpdated'),
                  cell: (r) => (r.updatedAt ? new Date(r.updatedAt).toLocaleDateString() : '-'),
                },
              ]}
              empty={
                <CloudscapeBox textAlign="center" padding="m">
                  <b>{t('registry.noApproved')}</b>
                  <CloudscapeBox variant="p" color="text-body-secondary" padding={{ top: 'xs' }}>
                    {t('registry.noApprovedHint')}
                  </CloudscapeBox>
                </CloudscapeBox>
              }
            />
          </SpaceBetween>
        </Modal>
      )}

      {/* Delete confirmation */}
      {deleteTarget && (
        <Modal
          visible
          onDismiss={() => setDeleteTarget(null)}
          header={t('skills.deleteSkill')}
          footer={
            <CloudscapeBox float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setDeleteTarget(null)}>{t('skills.cancel')}</Button>
                <Button variant="primary" onClick={handleDelete}>{t('skills.delete')}</Button>
              </SpaceBetween>
            </CloudscapeBox>
          }
        >
          <p>
            {t('skills.deleteConfirm')} <strong>{deleteTarget.skillName}</strong>{' '}
            {t('skills.forUserScope')} <strong>{displayUserId(deleteTarget.userId)}</strong>?
          </p>
        </Modal>
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
        <Table
          loading={isLoading}
          loadingText={t('skills.loading')}
          items={skills}
          trackBy={(s) => `${s.userId}:${s.skillName}`}
          columnDefinitions={[
            { id: 'name', header: t('skills.colName'), cell: (s) => s.skillName },
            { id: 'description', header: t('skills.colDescription'), cell: (s) => s.description },
            {
              id: 'tools',
              header: t('skills.colTools'),
              cell: (s) => (s.allowedTools || []).join(', ') || '-',
            },
            {
              id: 'updated',
              header: t('skills.colUpdated'),
              cell: (s) => (s.updatedAt ? new Date(s.updatedAt).toLocaleDateString() : '-'),
            },
            {
              id: 'actions',
              header: t('skills.colActions'),
              minWidth: 180,
              cell: (s) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={() => handleEdit(s)}>{t('skills.edit')}</Button>
                  <Button onClick={() => setDeleteTarget(s)}>{t('skills.delete')}</Button>
                </SpaceBetween>
              ),
            },
          ]}
          empty={
            <CloudscapeBox textAlign="center" padding="m">
              <b>{t('skills.noSkills')}</b>
              <CloudscapeBox variant="p" color="text-body-secondary" padding={{ top: 'xs' }}>
                {t('skills.noSkillsHint')}
              </CloudscapeBox>
            </CloudscapeBox>
          }
        />
      )}
      </>
        );
      })()}

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

      {/* Agent Prompt Tab */}
      {activeTab === 'agentPrompts' && (
        <AgentPromptTab
          error={error}
          success={success}
          clearMessages={clearMessages}
          setError={setError}
          setSuccess={setSuccess}
          cognitoUsers={cognitoUsers}
        />
      )}

      {/* Sessions Tab */}
      {activeTab === 'sessions' && (
        <SpaceBetween size="l">
          {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
          {success && <Alert type="success">{success}</Alert>}

          <Table
            header={
              <CloudscapeHeader
                variant="h2"
                actions={
                  <Button iconName="refresh" onClick={loadSessions}>
                    {t('sessions.refresh')}
                  </Button>
                }
              >
                {t('sessions.title')}
              </CloudscapeHeader>
            }
            loading={sessionsLoading}
            loadingText={t('sessions.loading')}
            items={sessions}
            // SessionId alone isn't unique — a user's text and voice sessions
            // share the same sessionId (derived from the JWT sub). Compose a
            // key that includes kind so React diffing stays stable.
            trackBy={(s) => `${s.kind || 'text'}:${s.sessionId}`}
            columnDefinitions={[
              {
                id: 'userId',
                header: t('sessions.colUserId'),
                cell: (s) => (
                  <span title={s.userId}>
                    {s.userId.length > 24 ? s.userId.slice(0, 24) + '...' : s.userId}
                  </span>
                ),
              },
              {
                id: 'kind',
                header: t('sessions.colKind'),
                cell: (s) => (s.kind === 'voice' ? t('sessions.kindVoice') : t('sessions.kindText')),
              },
              {
                id: 'sessionId',
                header: t('sessions.colSessionId'),
                cell: (s) => (
                  <span title={s.sessionId}>
                    {s.sessionId.length > 36 ? s.sessionId.slice(0, 36) + '...' : s.sessionId}
                  </span>
                ),
              },
              {
                id: 'lastActive',
                header: t('sessions.colLastActive'),
                cell: (s) => (s.lastActiveAt ? new Date(s.lastActiveAt).toLocaleString() : '-'),
              },
              {
                id: 'tokens7d',
                header: t('sessions.colTokens7d'),
                cell: (s) => (typeof s.totalTokens7d === 'number' ? s.totalTokens7d.toLocaleString() : '-'),
              },
              {
                id: 'actions',
                header: t('sessions.colActions'),
                minWidth: 220,
                cell: (s) => (
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      onClick={() => setShellTarget({
                        userId: s.userId,
                        sessionId: s.sessionId,
                        kind: s.kind ?? 'text',
                      })}
                    >
                      {t('sessions.shell')}
                    </Button>
                    <Button onClick={() => handleStopSession(s.sessionId, s.kind)}>
                      {t('sessions.stop')}
                    </Button>
                  </SpaceBetween>
                ),
              },
            ]}
            empty={
              <CloudscapeBox textAlign="center" padding="m">
                <b>{t('sessions.noSessions')}</b>
                <CloudscapeBox variant="p" color="text-body-secondary" padding={{ top: 'xs' }}>
                  {t('sessions.noSessionsHint')}
                </CloudscapeBox>
              </CloudscapeBox>
            }
          />
          {shellTarget && (
            <ShellModal target={shellTarget} onClose={() => setShellTarget(null)} />
          )}
        </SpaceBetween>
      )}

      {/* Tool Access Tab */}
      {activeTab === 'users' && (
        <SpaceBetween size="l">
          {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
          {success && <Alert type="success">{success}</Alert>}

          <Container header={<CloudscapeHeader variant="h3">{t('users.policyEngine')}</CloudscapeHeader>}>
            <SpaceBetween direction="horizontal" size="s">
              <div style={{ minWidth: 180 }}>
                <Select
                  selectedOption={
                    policyMode === 'ENFORCE'
                      ? { value: 'ENFORCE', label: t('users.enforce') }
                      : { value: 'LOG_ONLY', label: t('users.logOnly') }
                  }
                  onChange={({ detail }) => setPolicyMode(detail.selectedOption.value as 'ENFORCE' | 'LOG_ONLY')}
                  options={[
                    { value: 'ENFORCE', label: t('users.enforce') },
                    { value: 'LOG_ONLY', label: t('users.logOnly') },
                  ]}
                />
              </div>
              {policyMode === 'ENFORCE' ? (
                <StatusIndicator type="success">{t('users.enforced')}</StatusIndicator>
              ) : (
                <StatusIndicator type="warning">{t('users.auditOnly')}</StatusIndicator>
              )}
            </SpaceBetween>
          </Container>

          {selectedPermUser && (
            <Container
              header={
                <CloudscapeHeader
                  variant="h2"
                  description={
                    <>
                      {t('users.toolPermsHint')} {t('users.actorId')}{' '}
                      <code>{getActorId(selectedPermUser)}</code>
                    </>
                  }
                >
                  {t('users.toolPermsFor')} {selectedPermUser.email || selectedPermUser.username}
                </CloudscapeHeader>
              }
            >
              <SpaceBetween size="m">
                {gatewayTools.length === 0 ? (
                  <CloudscapeBox textAlign="center" padding="m">
                    <b>{t('users.noTools')}</b>
                  </CloudscapeBox>
                ) : (
                  <>
                    <SpaceBetween direction="horizontal" size="xs">
                      <Button
                        onClick={() => {
                          const all: Record<string, boolean> = {};
                          for (const tt of gatewayTools) all[tt.name] = true;
                          setUserToolSelections(all);
                        }}
                      >
                        {t('users.selectAll')}
                      </Button>
                      <Button
                        onClick={() => {
                          const none: Record<string, boolean> = {};
                          for (const tt of gatewayTools) none[tt.name] = false;
                          setUserToolSelections(none);
                        }}
                      >
                        {t('users.deselectAll')}
                      </Button>
                    </SpaceBetween>
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
                          {tool.source === 'builtin' ? (
                            <Badge color="green">{t('users.toolSourceBuiltin')}</Badge>
                          ) : (
                            <Badge color="blue">{t('users.toolSourceGateway')}</Badge>
                          )}
                          <span className="perm-tool-desc">{tool.description}</span>
                          <span className="perm-tool-target">{tool.targetName}</span>
                        </label>
                      ))}
                    </div>
                  </>
                )}

                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={handleCancelPermissions}>{t('users.cancel')}</Button>
                  <Button
                    variant="primary"
                    onClick={handleSavePermissions}
                    disabled={!permIsDirty || permSaving}
                    loading={permSaving}
                  >
                    {t('users.savePermissions')}
                  </Button>
                </SpaceBetween>
              </SpaceBetween>
            </Container>
          )}

          {!selectedPermUser && (
            <Table
              header={
                <CloudscapeHeader
                  variant="h2"
                  actions={
                    <Button iconName="refresh" onClick={loadCognitoUsers}>
                      {t('users.refresh')}
                    </Button>
                  }
                >
                  {t('users.perUserPerms')}
                </CloudscapeHeader>
              }
              loading={usersLoading}
              loadingText={t('users.loadingUsers')}
              items={cognitoUsers}
              trackBy="sub"
              columnDefinitions={[
                { id: 'email', header: t('users.colEmail'), cell: (u) => u.email || u.username },
                {
                  id: 'userId',
                  header: t('users.colUserId'),
                  cell: (u) => (
                    <span title={u.sub}>{u.sub.length > 28 ? u.sub.slice(0, 28) + '...' : u.sub}</span>
                  ),
                },
                {
                  id: 'status',
                  header: t('users.colStatus'),
                  cell: (u) =>
                    u.status === 'CONFIRMED' ? (
                      <StatusIndicator type="success">{u.status}</StatusIndicator>
                    ) : (
                      <StatusIndicator type="stopped">{u.status}</StatusIndicator>
                    ),
                },
                {
                  id: 'groups',
                  header: t('users.colGroups'),
                  cell: (u) =>
                    u.groups.length > 0 ? (
                      <SpaceBetween direction="horizontal" size="xxs">
                        {u.groups.map((g) => (
                          <Badge key={g} color={g === 'admin' ? 'red' : 'blue'}>
                            {g}
                          </Badge>
                        ))}
                      </SpaceBetween>
                    ) : (
                      '-'
                    ),
                },
                {
                  id: 'demo',
                  header: t('users.colDemo'),
                  minWidth: 280,
                  cell: (u) => {
                    const cfg = getConfig();
                    const loginHint = u.email || u.username;
                    const chatUrl = cfg.chatbotUrl
                      ? `${cfg.chatbotUrl.replace(/\/$/, '')}/?username=${encodeURIComponent(loginHint)}`
                      : '';
                    const simUrl = cfg.deviceSimulatorUrl
                      ? `${cfg.deviceSimulatorUrl.replace(/\/$/, '')}/?userId=${encodeURIComponent(u.sub)}`
                      : '';
                    return (
                      <SpaceBetween direction="horizontal" size="xxs">
                        {chatUrl && (
                          <Button iconName="external" iconAlign="right" href={chatUrl} target="_blank">
                            {t('users.openChatbot')}
                          </Button>
                        )}
                        {simUrl && (
                          <Button iconName="external" iconAlign="right" href={simUrl} target="_blank">
                            {t('users.openDeviceSim')}
                          </Button>
                        )}
                        {!chatUrl && !simUrl && <span>-</span>}
                      </SpaceBetween>
                    );
                  },
                },
                {
                  id: 'actions',
                  header: t('users.colActions'),
                  minWidth: 200,
                  cell: (u) => (
                    <Button variant="primary" onClick={() => handleManagePermissions(u)}>
                      {t('users.managePermissions')}
                    </Button>
                  ),
                },
              ]}
              empty={
                <CloudscapeBox textAlign="center" padding="m">
                  <b>{t('users.noUsers')}</b>
                </CloudscapeBox>
              }
            />
          )}
        </SpaceBetween>
      )}

      {/* Integration Registry Tab */}
      {activeTab === 'integrations' && (
        <SpaceBetween size="l">
          <SegmentedControl
            selectedId={integrationsSubTab}
            onChange={({ detail }) => setIntegrationsSubTab(detail.selectedId as 'overview' | 'a2a')}
            options={[
              { id: 'overview', text: t('integrations.sub.overview') },
              { id: 'a2a', text: t('integrations.sub.a2a') },
              { id: 'mcp', text: `${t('integrations.sub.mcp')} · ${t('integrations.comingSoon')}`, disabled: true },
              { id: 'apigw', text: `${t('integrations.sub.apiGw')} · ${t('integrations.comingSoon')}`, disabled: true },
            ]}
          />

          {integrationsSubTab === 'overview' && (
            <SpaceBetween size="l">
              <Table
                header={
                  <CloudscapeHeader variant="h2" description={t('integrations.desc')}>
                    {t('integrations.title')}
                  </CloudscapeHeader>
                }
                items={[
                  { id: 'lambda', name: t('integrations.lambdaTargets'), description: t('integrations.lambdaDesc'), active: true },
                  { id: 'mcp', name: t('integrations.mcpServers'), description: t('integrations.mcpDesc'), active: false },
                  { id: 'a2a', name: t('integrations.a2aAgents'), description: t('integrations.a2aDesc'), active: true },
                  { id: 'api', name: t('integrations.apiGateway'), description: t('integrations.apiDesc'), active: false },
                ]}
                trackBy="id"
                columnDefinitions={[
                  { id: 'type', header: t('integrations.colType'), cell: (i) => i.name },
                  { id: 'description', header: t('integrations.colDescription'), cell: (i) => i.description },
                  {
                    id: 'status',
                    header: t('integrations.colStatus'),
                    cell: (i) =>
                      i.active ? (
                        <StatusIndicator type="success">{t('integrations.active')}</StatusIndicator>
                      ) : (
                        <StatusIndicator type="pending">{t('integrations.planned')}</StatusIndicator>
                      ),
                  },
                ]}
              />
              <Container header={<CloudscapeHeader variant="h3">{t('integrations.roadmap')}</CloudscapeHeader>}>
                <CloudscapeBox color="text-body-secondary">{t('integrations.roadmapDesc')}</CloudscapeBox>
              </Container>
            </SpaceBetween>
          )}

          {integrationsSubTab === 'a2a' && (
            <SpaceBetween size="l">
              {a2aError && <Alert type="error" dismissible onDismiss={() => setA2aError('')}>{a2aError}</Alert>}

              <Table
                header={
                  <CloudscapeHeader
                    variant="h2"
                    counter={`(${a2aAgents.length})`}
                    actions={
                      <Button
                        iconName="refresh"
                        onClick={() => {
                          setA2aLoading(true);
                          setA2aError('');
                          listA2aAgents()
                            .then(setA2aAgents)
                            .catch((err: any) => setA2aError(err.message))
                            .finally(() => setA2aLoading(false));
                        }}
                      >
                        {t('integrations.a2a.refresh')}
                      </Button>
                    }
                  >
                    {t('integrations.a2a.title')}
                  </CloudscapeHeader>
                }
                loading={a2aLoading}
                loadingText={t('common.loading')}
                items={a2aAgents}
                trackBy="recordId"
                columnDefinitions={[
                  { id: 'name', header: t('integrations.a2a.col.name'), cell: (r) => r.name },
                  { id: 'description', header: t('integrations.a2a.col.description'), cell: (r) => r.description },
                  {
                    id: 'endpoint',
                    header: t('integrations.a2a.col.endpoint'),
                    cell: (r) => (
                      <span title={r.card.url}>
                        {r.card.url && r.card.url.length > 48 ? r.card.url.slice(0, 48) + '…' : r.card.url}
                      </span>
                    ),
                  },
                  {
                    id: 'auth',
                    header: t('integrations.a2a.col.auth'),
                    cell: (r) => (r.card.authentication?.schemes || ['none'])[0],
                  },
                  {
                    id: 'tags',
                    header: t('integrations.a2a.col.tags'),
                    cell: (r) => {
                      const tags = r.card.tags || [];
                      const visible = tags.slice(0, 3);
                      const overflow = tags.length - visible.length;
                      return (
                        <SpaceBetween direction="horizontal" size="xxs">
                          {visible.map((tg) => <Badge key={tg}>{tg}</Badge>)}
                          {overflow > 0 && <Badge>+{overflow}</Badge>}
                        </SpaceBetween>
                      );
                    },
                  },
                  { id: 'publishedBy', header: t('integrations.a2a.col.publishedBy'), cell: (r) => r.publishedBy || '—' },
                  {
                    id: 'lastUpdated',
                    header: t('integrations.a2a.col.lastUpdated'),
                    cell: (r) => (r.updatedAt ? new Date(r.updatedAt).toLocaleString() : '-'),
                  },
                  {
                    id: 'actions',
                    header: t('integrations.a2a.col.actions'),
                    minWidth: 110,
                    cell: (r) => (
                      <Button onClick={() => setA2aDrawer(r)}>{t('integrations.a2a.view')}</Button>
                    ),
                  },
                ]}
                empty={
                  <CloudscapeBox textAlign="center" padding="m">
                    <b>{t('integrations.a2a.empty')}</b>
                    <CloudscapeBox variant="p" color="text-body-secondary" padding={{ top: 'xs' }}>
                      {t('integrations.a2a.emptyHint')}
                    </CloudscapeBox>
                  </CloudscapeBox>
                }
              />

              {a2aDrawer && (
                <Modal
                  visible
                  onDismiss={() => setA2aDrawer(null)}
                  header={a2aDrawer.name}
                  size="large"
                  footer={
                    <CloudscapeBox float="right">
                      <Button onClick={() => setA2aDrawer(null)}>{t('form.close')}</Button>
                    </CloudscapeBox>
                  }
                >
                  <SpaceBetween size="s">
                    <p>{a2aDrawer.description}</p>
                    <dl className="drawer-fields">
                      <dt>{t('integrations.a2a.drawer.endpoint')}</dt>
                      <dd>{a2aDrawer.card.url}</dd>
                      <dt>{t('integrations.a2a.drawer.version')}</dt>
                      <dd>{a2aDrawer.card.version}</dd>
                      <dt>{t('integrations.a2a.drawer.provider')}</dt>
                      <dd>{a2aDrawer.card.provider?.organization || '—'}</dd>
                      <dt>{t('integrations.a2a.drawer.auth')}</dt>
                      <dd>{(a2aDrawer.card.authentication?.schemes || []).join(', ') || 'none'}</dd>
                      <dt>{t('integrations.a2a.drawer.capabilities')}</dt>
                      <dd>
                        {(['streaming', 'pushNotifications', 'stateTransitionHistory'] as const)
                          .filter((k) => a2aDrawer.card.capabilities?.[k])
                          .join(', ') || '—'}
                      </dd>
                      <dt>{t('integrations.a2a.drawer.tags')}</dt>
                      <dd>{(a2aDrawer.card.tags || []).join(', ') || '—'}</dd>
                      <dt>{t('integrations.a2a.drawer.skills')}</dt>
                      <dd>
                        <ul className="drawer-skills">
                          {(a2aDrawer.card.skills || []).map((s) => (
                            <li key={s.id}>
                              <strong>{s.id}</strong> — {s.name}: {s.description}
                              {s.examples && s.examples.length > 0 && (
                                <ul>
                                  {s.examples.map((ex, i) => <li key={i}><em>{ex}</em></li>)}
                                </ul>
                              )}
                            </li>
                          ))}
                        </ul>
                      </dd>
                      <dt>{t('integrations.a2a.drawer.recordId')}</dt>
                      <dd><code>{a2aDrawer.recordId}</code></dd>
                      <dt>{t('integrations.a2a.drawer.createdAt')}</dt>
                      <dd>{a2aDrawer.createdAt ? new Date(a2aDrawer.createdAt).toLocaleString() : '-'}</dd>
                      <dt>{t('integrations.a2a.drawer.updatedAt')}</dt>
                      <dd>{a2aDrawer.updatedAt ? new Date(a2aDrawer.updatedAt).toLocaleString() : '-'}</dd>
                    </dl>
                  </SpaceBetween>
                </Modal>
              )}
            </SpaceBetween>
          )}
        </SpaceBetween>
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
        const items = [
          {
            id: 'evaluator',
            name: t('guardrails.evaluator'),
            description: t('guardrails.evaluatorDesc'),
            action: (
              <Button variant="primary" href={evaluatorUrl} target="_blank" iconAlign="right" iconName="external">
                {t('guardrails.openConsole')}
              </Button>
            ),
          },
          {
            id: 'bedrock',
            name: t('guardrails.bedrockGuardrails'),
            description: t('guardrails.bedrockDesc'),
            action: (
              <Button variant="primary" href="https://console.aws.amazon.com/bedrock/home#/guardrails" target="_blank" iconAlign="right" iconName="external">
                {t('guardrails.openConsole')}
              </Button>
            ),
          },
          {
            id: 'cedar',
            name: t('guardrails.cedarPolicy'),
            description: t('guardrails.cedarDesc'),
            action: (
              <Button onClick={() => setActiveTab('users')}>
                {t('guardrails.goToToolAccess')}
              </Button>
            ),
          },
        ];
        return (
          <Table
            header={
              <CloudscapeHeader variant="h2" description={t('guardrails.desc')}>
                {t('guardrails.title')}
              </CloudscapeHeader>
            }
            items={items}
            trackBy="id"
            columnDefinitions={[
              { id: 'name', header: t('guardrails.colGuardrail'), cell: (i) => i.name },
              { id: 'description', header: t('guardrails.colDescription'), cell: (i) => i.description },
              { id: 'action', header: t('guardrails.colAction'), minWidth: 220, cell: (i) => i.action },
            ]}
          />
        );
      })()}
    </div>
  );
};

export default AdminConsole;
