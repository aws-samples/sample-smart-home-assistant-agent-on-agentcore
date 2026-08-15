import React, { useState, useEffect, useCallback, useMemo } from 'react';
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
  createCognitoUser,
  addUserToAdminGroup,
  removeUserFromAdminGroup,
  deleteCognitoUser,
  listGatewayTools,
  getModelCatalog,
  CatalogModel,
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
  listRegistrySkills,
  RegistrySkill,
  reviewRegistryRecord,
  importRegistryRecords,
  listA2aAgents,
  A2AAgentRecord,
  cardAuthSchemes,
  A2AConformanceBlocked,
  A2AConformanceGate,
  checkA2aConformance,
  A2AConformanceRow,
  listA2aGrantsForRecord,
  A2AGrantSummary,
  getA2aManifest,
  A2aManifest,
  getAgentPrompts,
  saveAgentPrompt,
  deleteAgentPrompt,
  startRecommendation,
  listRecommendations,
  getRecommendation,
  deleteRecommendation,
  applyRecommendation,
  listAgentFleet,
  listScenarios,
  syncScenarioSchedules,
  exportScenes,
  importScenes,
  ScenarioRow,
  listBundles,
  listABTests,
  startABTest,
  stopABTest,
  getABToggle,
  setABToggle,
  FleetAgent,
  OptAgentType,
  OptRecommendation,
  OptBundle,
  OptABTestSummary,
  OptABExecutionStatus,
  OptABToggle,
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
import Link from '@cloudscape-design/components/link';
import Alert from '@cloudscape-design/components/alert';
import Autosuggest from '@cloudscape-design/components/autosuggest';
import Badge from '@cloudscape-design/components/badge';
import CloudscapeBox from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import FormField from '@cloudscape-design/components/form-field';
import CloudscapeHeader from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import Popover from '@cloudscape-design/components/popover';
import Select from '@cloudscape-design/components/select';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Table from '@cloudscape-design/components/table';
import Textarea from '@cloudscape-design/components/textarea';
import Toggle from '@cloudscape-design/components/toggle';
import Modal from '@cloudscape-design/components/modal';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import { getConfig } from '../config';
import { getCurrentUserEmail } from '../auth/CognitoAuth';
import { useI18n } from '../i18n';
import SubAgentPolicyPage from './SubAgentPolicy/SubAgentPolicyPage';
import { sanitizeActorId } from '../api/sanitizeActor';
import ShellModal, { ShellTarget } from './ShellModal';
import { EntryEnvironmentTable } from './Optimization/EntryEnvironmentTable';
import { DashboardSection } from './Dashboard/DashboardSection';
import { AgentsPage } from './AgentsPage';
import architectureDiagram from '../assets/architecture.drawio.png';

/**
 * Every routable tab, as a runtime value.
 *
 * A bare union type cannot be checked against `location.hash` at runtime, so a
 * deep link had no way to be validated and `#/agents` silently rendered the
 * overview instead. Deriving `ActiveTab` from this array keeps one list: adding a
 * tab here makes it both type-checkable and deep-linkable.
 */
export const ACTIVE_TABS = [
  'overview',
  'agents',
  'integrations',
  'models',
  'skills',
  'agentPrompts',
  'users',
  'subAgentPolicy',
  'memories',
  'identity',
  'instanceType',
  'sessions',
  'guardrails',
  'scenarios',
  'observability',
  'evaluations',
  'optimization',
  'knowledgeBase',
] as const;

export type ActiveTab = (typeof ACTIVE_TABS)[number];

/** The tab a hash names, or null when it names nothing we render. */
export function tabFromHash(hash: string): ActiveTab | null {
  const name = hash.replace(/^#\/?/, '');
  return (ACTIVE_TABS as readonly string[]).includes(name)
    ? (name as ActiveTab)
    : null;
}

interface ActorRow {
  actorId: string;
  email: string | null;
}

// Skill name validation (matches Strands SDK pattern)
const SKILL_NAME_RE = /^(?!-)(?!.*--)(?!.*-$)[a-z0-9-]{1,64}$/;

// The model picker's contents are no longer listed here. They come from
// `GET /settings/{userId}?action=catalog`, which merges bedrock-runtime's
// ListFoundationModels + ListInferenceProfiles with bedrock-mantle's
// OpenAI-compatible /models listing (cdk/lambda/admin-api/model_catalog.py).
//
// The hardcoded list was 33 entries that had to be hand-edited whenever Bedrock
// shipped a model, and it could not express which endpoint served an entry — which
// stopped being a cosmetic gap once the default model became one that Converse
// cannot reach at all. See docs/architecture-and-design.md section 8.8.

/** Where a user is. Stored on their `__settings__` row. */
interface UserPlace {
  timezone: string;
  latitude: number | null;
  longitude: number | null;
}

/** The same thing mid-edit. Coordinates are strings so a half-typed "-12." is a
 *  state the input can hold; they are parsed on save. */
interface PlaceDraft {
  timezone: string;
  latitude: string;
  longitude: string;
}

const EMPTY_PLACE_DRAFT: PlaceDraft = { timezone: '', latitude: '', longitude: '' };

/** A short list of IANA zones, not all 599 of them. These cover the demo's
 *  users; the field also accepts anything typed, and the API validates against
 *  the real tz database, so the list is a convenience rather than a whitelist. */
const COMMON_TIMEZONES = [
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Asia/Singapore',
  'Asia/Kolkata',
  'Europe/London',
  'Europe/Berlin',
  'America/New_York',
  'America/Chicago',
  'America/Los_Angeles',
  'Australia/Sydney',
  'UTC',
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
// Cognito group cell — shared by the Identity and Tool Policy tables
// ---------------------------------------------------------------------------

/** Prefix identifying a group that is an A2A sub-agent skill grant. Mirrors
 *  `shared/a2a_groups.GROUP_PREFIX`; a mismatch here only affects presentation. */
const A2A_GROUP_PREFIX = 'a2a-';

/** Split `a2a-<agent>.<skill>` into its two halves, or null for other groups. */
function parseA2aGroup(name: string): { agent: string; skill: string } | null {
  if (!name.startsWith(A2A_GROUP_PREFIX)) return null;
  const rest = name.slice(A2A_GROUP_PREFIX.length);
  const dot = rest.lastIndexOf('.');
  if (dot <= 0) return null;
  return { agent: rest.slice(0, dot), skill: rest.slice(dot + 1) };
}

interface GroupsCellProps {
  groups: string[];
  t: (key: string) => string;
}

/**
 * A user's Cognito groups, in one line.
 *
 * Rendered as a flat badge list until 2026-08-13, which was fine at two or three
 * groups. Granting the full A2A catalogue puts **21** on a user, and the column is
 * narrow enough that each badge wrapped onto its own line — a single row grew past
 * 900px and one user filled the viewport, so the table stopped being a table.
 *
 * Two changes, both about what an admin is actually scanning for:
 *
 *   - Role groups (`admin`, anything without the `a2a-` prefix) stay visible. They
 *     are few, and "is this person an admin" is the question the column exists to
 *     answer at a glance.
 *   - Skill grants collapse to one badge per SUB-AGENT with a count, because 21
 *     rows of `a2a-<agent>.<skill>` is the same information as "8 agents, 21
 *     skills" plus detail nobody reads in a table cell. The full list is one hover
 *     away in a popover, grouped by agent — which is more legible than the flat
 *     list ever was, since the flat list was not sorted.
 */
const GroupsCell: React.FC<GroupsCellProps> = ({ groups, t }) => {
  if (!groups || groups.length === 0) return <>-</>;

  const roleGroups: string[] = [];
  const byAgent = new Map<string, string[]>();
  for (const g of groups) {
    const parsed = parseA2aGroup(g);
    if (!parsed) {
      roleGroups.push(g);
      continue;
    }
    const list = byAgent.get(parsed.agent) || [];
    list.push(parsed.skill);
    byAgent.set(parsed.agent, list);
  }

  const agents = [...byAgent.entries()].sort(([a], [b]) => a.localeCompare(b));
  const totalSkills = agents.reduce((n, [, skills]) => n + skills.length, 0);

  return (
    <SpaceBetween direction="horizontal" size="xxs">
      {roleGroups.sort().map((g) => (
        <Badge key={g} color={g === 'admin' ? 'red' : 'grey'}>{g}</Badge>
      ))}
      {agents.length > 0 && (
        <Popover
          dismissButton={false}
          position="top"
          size="large"
          triggerType="text"
          header={t('users.a2aGrantsHeader')}
          content={
            <SpaceBetween size="xs">
              {agents.map(([agent, skills]) => (
                <div key={agent}>
                  <Badge color="blue">{agent}</Badge>{' '}
                  <span style={{ fontSize: '12px' }}>{skills.sort().join(', ')}</span>
                </div>
              ))}
            </SpaceBetween>
          }
        >
          <Badge color="blue">
            {t('users.a2aGrantsSummary')
              .replace('{agents}', String(agents.length))
              .replace('{skills}', String(totalSkills))}
          </Badge>
        </Popover>
      )}
    </SpaceBetween>
  );
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
  // The picker's contents, fetched live from both Bedrock endpoints rather than
  // hardcoded here. `catalogError` is why the list can be short: a failed listing
  // and an account with nothing enabled are otherwise the same empty dropdown.
  const [catalog, setCatalog] = useState<CatalogModel[]>([]);
  const [catalogDefaultId, setCatalogDefaultId] = useState('');
  const [catalogError, setCatalogError] = useState('');
  const { t } = useI18n();

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      // Before the settings, because the endpoint each saved model resolves to
      // comes from here — a save must not write a modelId with no endpoint beside
      // it just because the catalog had not arrived yet.
      try {
        const cat = await getModelCatalog();
        setCatalog(cat.models || []);
        setCatalogDefaultId(cat.defaultModelId || '');
        setCatalogError(cat.catalogError || '');
      } catch (err: any) {
        setCatalog([]);
        setCatalogError(err.message || String(err));
      }

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

  /** The endpoint serving a model id, from the catalog we just loaded.
   *
   *  Sent with the save so the agent reads it instead of resolving it on a cold
   *  start. Empty when the catalog could not be loaded or does not know the id;
   *  the API treats that as "unset" and the agent falls back to its own lookup,
   *  which is why a missing catalog does not block saving. */
  const endpointFor = (modelId: string) =>
    catalog.find((m) => m.id === modelId)?.endpoint ?? '';

  const handleSaveGlobal = async () => {
    clearMessages();
    try {
      await updateSettings('__global__', {
        modelId: globalModelId,
        modelEndpoint: endpointFor(globalModelId),
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
      await updateSettings(userId, {
        modelId: newModel,
        modelEndpoint: endpointFor(newModel),
        visionModelId: newVisionModel,
      });
      setSavedUserModels((prev) => ({ ...prev, [user.sub]: newModel }));
      setSavedUserVisionModels((prev) => ({ ...prev, [user.sub]: newVisionModel }));
      setSuccess(t('models.userUpdated').replace('{user}', user.email || user.username || ''));
    } catch (err: any) {
      setError(err.message);
    }
  };

  /** Catalog entries as Cloudscape option groups, one group per provider.
   *
   *  The endpoint is on the label rather than hidden, because it is the field that
   *  decides which code path a turn takes and it is the first thing to check when
   *  a model misbehaves. The model id is in the description for the same reason:
   *  the label is a best-effort display string for Mantle models, the id is what
   *  the docs and the logs use. */
  const optionsFor = (predicate: (m: CatalogModel) => boolean, emptyLabel: string) => {
    const byProvider = new Map<string, CatalogModel[]>();
    for (const m of catalog) {
      if (!predicate(m)) continue;
      const list = byProvider.get(m.provider) ?? [];
      list.push(m);
      byProvider.set(m.provider, list);
    }
    return [
      { value: '', label: emptyLabel },
      ...[...byProvider.entries()].map(([provider, models]) => ({
        label: provider,
        options: models.map((m) => ({
          value: m.id,
          label: m.deprecated ? `${m.label} (${t('models.deprecated')})` : m.label,
          description: `${m.id} · ${m.endpoint}`,
        })),
      })),
    ];
  };

  const modelOptions = optionsFor(() => true, t('models.notSet'));
  const userModelOptions = optionsFor(() => true, t('models.useGlobalDefault'));
  // Only models the catalog states accept image input. Mantle models never
  // qualify: that listing returns ids only, so their capability is unknown rather
  // than absent, and the vision path runs on Converse either way.
  const visionModelOptions = optionsFor((m) => m.vision, t('models.notSet'));
  const userVisionModelOptions = optionsFor((m) => m.vision, t('models.useGlobalDefault'));

  /** The selected option for a stored id.
   *
   *  Falls back to a synthetic option rather than to the first entry, so a model
   *  the catalog no longer offers is shown as itself with a warning instead of
   *  silently reading as "Not set" — which would invite an admin to save and
   *  thereby wipe a working configuration. */
  const findOption = (
    opts: ReturnType<typeof optionsFor>,
    value: string,
  ): { value: string; label: string; description?: string } => {
    if (!value) return opts[0] as any;
    for (const entry of opts) {
      if ('options' in entry) {
        const hit = (entry as any).options.find((o: any) => o.value === value);
        if (hit) return hit;
      } else if ((entry as any).value === value) {
        return entry as any;
      }
    }
    return { value, label: `${value} (${t('models.notInCatalog')})`, description: value };
  };

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
      {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}

      {/* A short list because a listing FAILED is a different situation from an
          account with nothing enabled, and only one of them is worth an admin's
          time in the Bedrock console. Warning rather than error: whatever did load
          is still selectable. */}
      {catalogError && (
        <Alert type="warning" header={t('models.catalogFailedHeader')}>
          {t('models.catalogFailed').replace('{error}', catalogError)}
        </Alert>
      )}

      <Container
        header={
          <CloudscapeHeader
            variant="h2"
            description={t('models.globalHint')}
            actions={
              <Button
                iconName="refresh"
                onClick={async () => {
                  clearMessages();
                  try {
                    const cat = await getModelCatalog(true);
                    setCatalog(cat.models || []);
                    setCatalogDefaultId(cat.defaultModelId || '');
                    setCatalogError(cat.catalogError || '');
                    setSuccess(t('models.catalogRefreshed')
                      .replace('{n}', String((cat.models || []).length)));
                  } catch (err: any) {
                    setError(err.message);
                  }
                }}
              >
                {t('models.refreshCatalog')}
              </Button>
            }
          >
            {t('models.globalDefault')}
          </CloudscapeHeader>
        }
      >
        <SpaceBetween size="s">
          <FormField
            label={t('models.textModelLabel')}
            // What actually runs when this is unset. Without it "Not set" says
            // nothing about which model users are talking to.
            description={
              catalogDefaultId
                ? t('models.envDefaultHint').replace('{model}', catalogDefaultId)
                : undefined
            }
          >
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
// Scenarios Tab — every saved automation, across users
// ---------------------------------------------------------------------------
interface ScenariosTabProps {
  error: string;
  success: string;
  clearMessages: () => void;
  setError: (msg: string) => void;
  setSuccess: (msg: string) => void;
}

/**
 * Read-only, on purpose. A scene is created by an agent at the user's request, so
 * an operator's questions here are "what exists" and "did it run" — not "let me
 * author one". The one write is Reconcile, which is idempotent and self-healing.
 *
 * `lastRunAt` / `lastRunOk` are the point of the page. A scene that fires at 07:30
 * has no one watching it, so the run record is the only way to distinguish "works"
 * from "has never worked".
 */
const ScenariosTab: React.FC<ScenariosTabProps> = ({
  error, success, clearMessages, setError, setSuccess,
}) => {
  const [rows, setRows] = useState<ScenarioRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  // Scenes as code (spec 5 S6). The one authoring path on this otherwise
  // read-only page, and it is deliberately a paste box rather than a form: the
  // audience is a developer who already has the JSON, and a form would be a
  // second, worse scene editor competing with the agent that owns authoring.
  const [selected, setSelected] = useState<ScenarioRow[]>([]);
  const [codeOpen, setCodeOpen] = useState(false);
  const [codeUser, setCodeUser] = useState('');
  const [codeText, setCodeText] = useState('');
  const [codeBusy, setCodeBusy] = useState(false);
  const { t } = useI18n();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await listScenarios());
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [setError]);

  useEffect(() => { load(); }, [load]);

  const handleSync = async () => {
    clearMessages();
    setSyncing(true);
    try {
      const out = await syncScenarioSchedules();
      const counts = [
        `${(out.created || []).length} created`,
        `${(out.updated || []).length} updated`,
        `${(out.deleted || []).length} removed`,
      ].join(', ');
      if (out.failed && out.failed.length > 0) {
        // Surfaced as a warning rather than swallowed: a scene that cannot be
        // scheduled will not fire, and the reason (usually a sunrise trigger with
        // no coordinates for the owner) is actionable.
        setError(
          `${t('scenarios.syncPartial')} ${counts}. ` +
          out.failed.map((f) => `${f.name}: ${f.error}`).join('; ')
        );
      } else {
        setSuccess(`${t('scenarios.syncDone')} ${counts}.`);
      }
      await load();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSyncing(false);
    }
  };

  // The owner of the selected row, or the first row's owner. Prefilled rather than
  // demanded: scenes are per-user, and typing a Cognito sub by hand is exactly the
  // step where an operator exports the wrong person's automations.
  const defaultCodeUser = () => selected[0]?.userId || rows[0]?.userId || '';

  const openCode = () => {
    clearMessages();
    setCodeUser(defaultCodeUser());
    setCodeText('');
    setCodeOpen(true);
  };

  const handleExport = async () => {
    clearMessages();
    setCodeBusy(true);
    try {
      const out = await exportScenes(codeUser.trim());
      setCodeText(JSON.stringify(out.scenes, null, 2));
      if (out.count === 0) {
        setError(t('scenarios.codeEmpty'));
      } else {
        setSuccess(`${t('scenarios.codeExported')} ${out.count}`);
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setCodeBusy(false);
    }
  };

  const handleImport = async () => {
    clearMessages();
    let scenes: any;
    try {
      scenes = JSON.parse(codeText);
    } catch (err: any) {
      // Reported here rather than sent to the API: a JSON syntax error is the
      // paste's problem, and a round trip would only rename it.
      setError(`${t('scenarios.codeBadJson')} ${err.message}`);
      return;
    }
    // Accept either the bare array or the whole export envelope, because the
    // obvious thing to paste back is exactly what Export handed over.
    if (!Array.isArray(scenes) && Array.isArray(scenes?.scenes)) scenes = scenes.scenes;
    if (!Array.isArray(scenes)) {
      setError(t('scenarios.codeNotArray'));
      return;
    }
    setCodeBusy(true);
    try {
      const out = await importScenes(codeUser.trim(), scenes);
      if (out.failedCount > 0) {
        // Both counts, because a partial import is the normal outcome of editing
        // by hand and the operator needs to know which entries to fix.
        setError(
          `${out.createdCount} imported, ${out.failedCount} failed — ` +
          out.failed.map((f) => `#${f.index + 1} ${f.name || ''}: ${f.error}`).join('; ')
        );
      } else {
        setSuccess(`${t('scenarios.codeImported')} ${out.createdCount}. ${out.note || ''}`);
      }
      if (out.createdCount > 0) {
        setCodeOpen(false);
        await load();
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setCodeBusy(false);
    }
  };

  const runIndicator = (row: ScenarioRow) => {
    if (!row.lastRunAt) {
      return <StatusIndicator type="pending">{t('scenarios.neverRun')}</StatusIndicator>;
    }
    const when = new Date(row.lastRunAt).toLocaleString();
    return row.lastRunOk === false ? (
      <StatusIndicator type="error">{when}</StatusIndicator>
    ) : (
      <StatusIndicator type="success">{when}</StatusIndicator>
    );
  };

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
      {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}

      <Table
        header={
          <CloudscapeHeader
            variant="h2"
            description={t('scenarios.desc')}
            counter={rows.length > 0 ? `(${rows.length})` : undefined}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={load}>{t('overview.refresh')}</Button>
                <Button onClick={openCode}>{t('scenarios.asCode')}</Button>
                <Button variant="primary" loading={syncing} onClick={handleSync}>
                  {t('scenarios.reconcile')}
                </Button>
              </SpaceBetween>
            }
          >
            {t('scenarios.title')}
          </CloudscapeHeader>
        }
        loading={loading}
        loadingText={t('scenarios.loading')}
        items={rows}
        trackBy={(row) => `${row.userId}#${row.scenarioId}`}
        columnDefinitions={[
          {
            id: 'name',
            header: t('scenarios.colName'),
            cell: (row) => (
              <SpaceBetween size="xxs">
                <b>{row.name || row.scenarioId}</b>
                {row.description ? (
                  <span style={{ fontSize: '0.85em', opacity: 0.75 }}>{row.description}</span>
                ) : null}
              </SpaceBetween>
            ),
          },
          { id: 'user', header: t('scenarios.colUser'), cell: (row) => row.userId },
          {
            id: 'trigger',
            header: t('scenarios.colTrigger'),
            cell: (row) => (
              <SpaceBetween direction="horizontal" size="xxs">
                <Badge color={row.trigger?.sceneType === 'manual' ? 'grey' : 'blue'}>
                  {row.trigger?.sceneType || '-'}
                </Badge>
                <span>{row.triggerDescription}</span>
              </SpaceBetween>
            ),
          },
          {
            // The cron AND its timezone. A bare "cron(0 15 ...)" is unreadable:
            // 15:00 UTC is 23:00 in Shanghai and 07:00 in Los Angeles, and the
            // zone is what tells them apart.
            id: 'schedule',
            header: t('scenarios.colSchedule'),
            cell: (row) => (row.scheduled
              ? <span><code>{row.cron}</code> {row.timezone ? `(${row.timezone})` : ''}</span>
              : <span style={{ opacity: 0.6 }}>{t('scenarios.noSchedule')}</span>),
          },
          {
            id: 'actions',
            header: t('scenarios.colActions'),
            cell: (row) => row.actionCount,
          },
          {
            id: 'active',
            header: t('scenarios.colActive'),
            cell: (row) => (row.isActive
              ? <StatusIndicator type="success">{t('scenarios.active')}</StatusIndicator>
              : <StatusIndicator type="stopped">{t('scenarios.inactive')}</StatusIndicator>),
          },
          {
            id: 'lastRun',
            header: t('scenarios.colLastRun'),
            minWidth: 200,
            cell: (row) => (
              <SpaceBetween size="xxs">
                {runIndicator(row)}
                {row.lastRunDetail ? (
                  <span style={{ fontSize: '0.85em', opacity: 0.75 }}>{row.lastRunDetail}</span>
                ) : null}
              </SpaceBetween>
            ),
          },
          { id: 'source', header: t('scenarios.colSource'), cell: (row) => row.source || '-' },
        ]}
        selectionType="single"
        selectedItems={selected}
        onSelectionChange={({ detail }) => setSelected(detail.selectedItems)}
        empty={
          <CloudscapeBox textAlign="center" padding="m">
            <b>{t('scenarios.empty')}</b>
            <CloudscapeBox variant="p" padding={{ top: 'xs' }}>
              {t('scenarios.emptyHint')}
            </CloudscapeBox>
          </CloudscapeBox>
        }
      />

      <Modal
        visible={codeOpen}
        onDismiss={() => setCodeOpen(false)}
        header={t('scenarios.asCode')}
        size="large"
        footer={
          <CloudscapeBox float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setCodeOpen(false)}>{t('skills.cancel')}</Button>
              <Button loading={codeBusy} disabled={!codeUser.trim()}
                      onClick={handleExport}>
                {t('scenarios.codeExport')}
              </Button>
              <Button variant="primary" loading={codeBusy}
                      disabled={!codeUser.trim() || !codeText.trim()}
                      onClick={handleImport}>
                {t('scenarios.codeImport')}
              </Button>
            </SpaceBetween>
          </CloudscapeBox>
        }
      >
        <SpaceBetween size="m">
          <Alert type="info">{t('scenarios.codeHelp')}</Alert>
          <FormField label={t('scenarios.codeUser')}
                     description={t('scenarios.codeUserHint')}>
            <Input value={codeUser} onChange={({ detail }) => setCodeUser(detail.value)}
                   placeholder="user@example.com" />
          </FormField>
          <FormField label={t('scenarios.codeJson')}
                     description={t('scenarios.codeJsonHint')}>
            <Textarea value={codeText} rows={16}
                      onChange={({ detail }) => setCodeText(detail.value)}
                      placeholder='[{"name": "Movie mode", "trigger": {"sceneType": "manual"}, "deviceActions": [...]}]' />
          </FormField>
        </SpaceBetween>
      </Modal>
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

export interface PromptEditorCardProps {
  agentType: AgentType;
  title: string;
  hint: string;
  /**
   * Editor height. Defaulted from the agent's prompt size rather than fixed:
   * the voice prompt is a dozen lines and the orchestrator's is ten times that,
   * so one height is either cramped or mostly blank.
   */
  rows?: number;
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

export const PromptEditorCard: React.FC<PromptEditorCardProps> = ({
  agentType,
  title,
  hint,
  rows,
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

  const editorRows = rows ?? (isGlobalScope
    ? agentType === 'voice' ? 12 : 18
    : agentType === 'voice' ? 8 : 10);

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

          <Alert type="info" header={t('prompts.optimizationCardTitle')}>
            {t('prompts.optimizationCardDesc')}{' '}
            <Link onFollow={(e) => {
              e.preventDefault();
              window.location.hash = '#/optimization';
            }} href="#/optimization">
              {t('prompts.optimizationCardLink')}
            </Link>
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
      {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}

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

const MemoriesTab: React.FC<MemoriesTabProps> = ({ error, success, setError, setSuccess, clearMessages }) => {
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
      {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}

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
      {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}

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
// Optimization Tab — AgentCore Optimization (recommendations, bundles, A/B).
// See docs/superpowers/specs/2026-05-17-agentcore-optimization-target-based-design.md
// for the target-based redesign (replaces the original config-bundle path).
// ---------------------------------------------------------------------------

const EVALUATORS = [
  { value: 'arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate', label: 'GoalSuccessRate' },
  { value: 'arn:aws:bedrock-agentcore:::evaluator/Builtin.Helpfulness', label: 'Helpfulness' },
  { value: 'arn:aws:bedrock-agentcore:::evaluator/Builtin.Correctness', label: 'Correctness' },
];

// AgentTypes the redesigned UI exposes. Voice was removed because the
// optimization gateway proxies HTTP only — voice traffic stays on its
// dedicated runtime via WSS direct-to-runtime. See spec §2.3.
// `text` and `tool_desc` are the built-in targets; any other value is a deployed
// agent's id, which the backend resolves to that agent's own runtime. A closed
// union here would mean editing the frontend every time a specialist is deployed,
// which is the config.js problem this page was supposed to avoid.
type OptUiAgentType = 'text' | 'tool_desc' | (string & {});

interface OptimizationTabProps {
  error: string;
  success: string;
  setError: (m: string) => void;
  setSuccess: (m: string) => void;
  cognitoUsers: CognitoUserInfo[];
}

const OptimizationTab: React.FC<OptimizationTabProps> = ({
  error, success, setError, setSuccess, cognitoUsers,
}) => {
  const { t } = useI18n();
  const [scope, setScope] = useState<string>('__global__');
  const [agentType, setAgentType] = useState<OptUiAgentType>('text');
  const [previewUnavailable, setPreviewUnavailable] = useState(false);
  // Optimisation targets: the two built-ins plus every deployed agent. Loaded
  // from the fleet so the list cannot drift from what is actually running.
  const [fleet, setFleet] = useState<FleetAgent[]>([]);

  const [recs, setRecs] = useState<OptRecommendation[]>([]);
  const [bundles, setBundles] = useState<OptBundle[]>([]);
  const [tests, setTests] = useState<OptABTestSummary[]>([]);
  const [toggle, setToggle] = useState<OptABToggle>({ enabled: false });
  const [toggleSaving, setToggleSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [showGenModal, setShowGenModal] = useState(false);
  const [showABModal, setShowABModal] = useState(false);
  const [selectedRec, setSelectedRec] = useState<OptRecommendation | null>(null);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [r, b, ab, tg] = await Promise.all([
        listRecommendations(scope),
        listBundles(scope, agentType as OptAgentType),
        listABTests(),
        getABToggle(),
      ]);
      setRecs(r);
      setBundles(b);
      setTests(ab);
      setToggle(tg);
      setPreviewUnavailable(false);
    } catch (e: any) {
      const msg = String(e?.message || e);
      if (msg.includes('AgentCoreOptimizationUnavailable')) {
        setPreviewUnavailable(true);
      } else {
        setError(msg);
      }
    } finally {
      setLoading(false);
    }
  }, [scope, agentType, setError]);

  useEffect(() => {
    // A failure here leaves the two built-in targets, which is the behaviour this
    // page had before — degraded, not broken.
    listAgentFleet().then(setFleet).catch(() => setFleet([]));
  }, []);

  const optTargets = useMemo(() => {
    const base = [
      { value: 'text', label: t('prompts.textAgent') },
      { value: 'tool_desc', label: t('optimization.toolDesc') },
    ];
    // Only runtimes can be optimised: a Gateway tool has no traces to analyse,
    // and the A/B variant runs the orchestrator's image so its prompt is the
    // orchestrator's.
    const agents = fleet
      .filter((a) => a.kind === 'specialist' || a.kind === 'voice')
      .map((a) => ({ value: a.agentId, label: a.displayName || a.agentId }));
    return [...base, ...agents];
  }, [fleet, t]);

  useEffect(() => { loadAll(); }, [loadAll]);

  // Polling: refresh non-terminal rows every 10s.
  useEffect(() => {
    const transient = ['NOT_STARTED', 'PAUSED', 'RUNNING'] as const;
    const hasTransient =
      recs.some((r) => r.status === 'PENDING' || r.status === 'IN_PROGRESS') ||
      tests.some((t) => (transient as readonly OptABExecutionStatus[]).includes(t.executionStatus));
    if (!hasTransient) return;
    const id = setInterval(() => { loadAll(); }, 10000);
    return () => clearInterval(id);
  }, [recs, tests, loadAll]);

  if (previewUnavailable) {
    return (
      <Alert type="warning" header={t('optimization.previewUnavailableTitle')}>
        {t('optimization.previewUnavailableDesc')}
      </Alert>
    );
  }

  const scopeOptions = [
    { value: '__global__', label: t('prompts.globalScope') },
    ...cognitoUsers.filter((u) => !!u.email).map((u) => ({ value: u.email!, label: u.email! })),
  ];

  const hasRunningTest = tests.some((t) => t.executionStatus === 'RUNNING' || t.executionStatus === 'NOT_STARTED' || t.executionStatus === 'PAUSED');

  const onToggleAB = async (next: boolean) => {
    if (!next && hasRunningTest) {
      const ok = window.confirm(t('optimization.abToggle.confirmStop'));
      if (!ok) return;
    }
    setToggleSaving(true);
    try {
      const r = await setABToggle(next);
      setSuccess(next
        ? t('optimization.abToggle.enabledMsg')
        : (r.stoppedTestId
            ? t('optimization.abToggle.disabledStoppedMsg').replace('{id}', r.stoppedTestId)
            : t('optimization.abToggle.disabledMsg'))
      );
      await loadAll();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setToggleSaving(false);
    }
  };

  return (
    <SpaceBetween size="l">
      <EntryEnvironmentTable />
      {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
      {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}

      <Container header={
        <CloudscapeHeader variant="h2" description={t('optimization.desc')}>
          {t('optimization.title')}
        </CloudscapeHeader>
      }>
        <SpaceBetween size="m">
          {/* A/B Routing toggle (global). When ON, chatbot text traffic
              flows through the dedicated optimization gateway and can be
              split between control + treatment runtime endpoints. When
              OFF, traffic routes 100% to the control endpoint. */}
          <FormField label={t('optimization.abToggle.label')}
                     description={t('optimization.abToggle.helpText')}>
            <SpaceBetween size="xs" direction="horizontal">
              <Toggle checked={toggle.enabled} disabled={toggleSaving}
                      onChange={({ detail }) => onToggleAB(detail.checked)}>
                {toggle.enabled ? t('optimization.abToggle.on') : t('optimization.abToggle.off')}
              </Toggle>
              {toggle.updatedAt && toggle.updatedBy && (
                <CloudscapeBox color="text-status-inactive" fontSize="body-s">
                  {t('optimization.abToggle.lastChangedBy')
                    .replace('{at}', new Date(toggle.updatedAt).toLocaleString())
                    .replace('{by}', toggle.updatedBy)}
                </CloudscapeBox>
              )}
            </SpaceBetween>
          </FormField>

          <SpaceBetween size="s" direction="horizontal">
            <FormField label={t('optimization.scope')}>
              <div style={{ minWidth: 240 }}>
                <Select
                  selectedOption={scopeOptions.find((o) => o.value === scope) ?? scopeOptions[0]}
                  options={scopeOptions}
                  onChange={({ detail }) => setScope(detail.selectedOption.value as string)}
                />
              </div>
            </FormField>
            <FormField label={t('optimization.agentType')}
                       description={t('optimization.agentTypeHint')}>
              {/* Options come from the fleet read model, so a newly deployed
                  sub-agent is optimisable without a frontend change. The two
                  built-ins stay first because they are what most runs target. */}
              <div style={{ maxWidth: 320 }}>
                <Select
                  selectedOption={
                    optTargets.find((o: { value: string }) => o.value === agentType)
                    ?? optTargets[0]
                  }
                  onChange={({ detail }) =>
                    setAgentType(detail.selectedOption.value as OptUiAgentType)}
                  options={optTargets}
                />
              </div>
            </FormField>
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      <Container header={
        <CloudscapeHeader variant="h2" actions={
          <Button variant="primary" onClick={() => setShowGenModal(true)}>
            {t('optimization.generate')}
          </Button>
        }>{t('optimization.recsTitle')}</CloudscapeHeader>
      }>
        <Table
          loading={loading}
          items={recs}
          columnDefinitions={[
            { id: 'id', header: t('optimization.col.name'), cell: (i: OptRecommendation) => i.recommendationId },
            { id: 'agent', header: t('optimization.col.agent'), cell: (i: OptRecommendation) => i.agentType },
            { id: 'eval', header: t('optimization.col.evaluator'), cell: (i: OptRecommendation) => i.evaluatorArn.split('/').pop() || '' },
            { id: 'status', header: t('optimization.col.status'), cell: (i: OptRecommendation) =>
              <StatusIndicator type={i.status === 'COMPLETED' ? 'success' : i.status === 'FAILED' ? 'error' : 'in-progress'}>{i.status}</StatusIndicator>
            },
            { id: 'created', header: t('optimization.col.created'), cell: (i: OptRecommendation) => new Date(i.createdAt).toLocaleString() },
            { id: 'applied', header: t('optimization.col.applied'), cell: (i: OptRecommendation) => i.appliedAt ? new Date(i.appliedAt).toLocaleString() : '—' },
            { id: 'actions', header: '', cell: (i: OptRecommendation) =>
              <SpaceBetween size="xs" direction="horizontal">
                <Button onClick={async () => {
                  try { setSelectedRec(await getRecommendation(i.recommendationId)); }
                  catch (e: any) { setError(e.message); }
                }}>{t('optimization.view')}</Button>
                <Button onClick={async () => {
                  if (!window.confirm(t('optimization.confirmDelete'))) return;
                  try { await deleteRecommendation(i.recommendationId); setSuccess(t('optimization.deleted')); loadAll(); }
                  catch (e: any) { setError(e.message); }
                }}>{t('optimization.delete')}</Button>
              </SpaceBetween>
            },
          ]}
          empty={<CloudscapeBox textAlign="center" padding="m">{t('optimization.recsEmpty')}</CloudscapeBox>}
        />
      </Container>

      {/* Tool description bundles — shown only when filter = tool_desc.
          Includes the "A/B not automated yet" Alert explainer. */}
      {agentType === 'tool_desc' && (
        <>
          <Alert type="info" header={t('optimization.toolDescAB.title')}>
            {t('optimization.toolDescAB.body')}
          </Alert>
          <Container header={<CloudscapeHeader variant="h2">{t('optimization.bundlesTitle')}</CloudscapeHeader>}>
            <Table
              loading={loading}
              items={bundles}
              columnDefinitions={[
                { id: 'name', header: t('optimization.col.bundle'), cell: (i: OptBundle) => i.bundleName },
                { id: 'agent', header: t('optimization.col.agent'), cell: (i: OptBundle) => i.agentType },
                { id: 'src', header: t('optimization.col.sourceRec'), cell: (i: OptBundle) => i.sourceRecommendationId || '—' },
                { id: 'latest', header: t('optimization.col.latest'), cell: (i: OptBundle) => i.latestVersionId },
                { id: 'created', header: t('optimization.col.created'), cell: (i: OptBundle) => new Date(i.createdAt).toLocaleString() },
              ]}
              empty={<CloudscapeBox textAlign="center" padding="m">{t('optimization.bundlesEmpty')}</CloudscapeBox>}
            />
          </Container>
        </>
      )}

      {/* A/B Tests — only shown for the text agent (target-based routing
          path). When the global toggle is OFF the Start button is
          disabled. */}
      {agentType === 'text' && (
        <Container header={
          <CloudscapeHeader variant="h2" actions={
            <Button variant="primary"
                    disabled={!toggle.enabled || hasRunningTest}
                    onClick={() => setShowABModal(true)}>
              {t('optimization.startAB')}
            </Button>
          }>{t('optimization.abTitle')}</CloudscapeHeader>
        }>
          {!toggle.enabled && (
            <Alert type="info">
              {t('optimization.abToggle.disabledStartHint')}
            </Alert>
          )}
          <Table
            loading={loading}
            items={tests}
            columnDefinitions={[
              { id: 'id', header: t('optimization.col.name'), cell: (i: OptABTestSummary) => i.testId },
              { id: 'status', header: t('optimization.col.status'), cell: (i: OptABTestSummary) =>
                <StatusIndicator type={
                  i.executionStatus === 'RUNNING' ? 'in-progress' :
                  i.executionStatus === 'STOPPED' ? 'stopped' :
                  'pending'
                }>{i.executionStatus}</StatusIndicator>
              },
              { id: 'winner', header: t('optimization.col.winner'), cell: (i: OptABTestSummary) => i.winner ?? '—' },
              { id: 'auto', header: t('optimization.col.autoStop'), cell: (i: OptABTestSummary) => new Date(i.autoStopAt).toLocaleString() },
              { id: 'actions', header: '', cell: (i: OptABTestSummary) =>
                <SpaceBetween size="xs" direction="horizontal">
                  {i.executionStatus === 'RUNNING' && <Button onClick={async () => {
                    try { await stopABTest(i.testId); setSuccess(t('optimization.stopped')); loadAll(); }
                    catch (e: any) { setError(e.message); }
                  }}>{t('optimization.stop')}</Button>}
                </SpaceBetween>
              },
            ]}
            empty={<CloudscapeBox textAlign="center" padding="m">{t('optimization.abEmpty')}</CloudscapeBox>}
          />
        </Container>
      )}

      {showGenModal && <GenerateRecommendationModal
        scope={scope} agentType={agentType}
        onClose={() => setShowGenModal(false)}
        onSubmitted={() => { setShowGenModal(false); loadAll(); }}
        setError={setError}
      />}
      {showABModal && <StartABTestModal
        onClose={() => setShowABModal(false)}
        onSubmitted={() => { setShowABModal(false); loadAll(); }}
        setError={setError}
      />}
      {selectedRec && <RecommendationDetailDrawer
        rec={selectedRec}
        onClose={() => setSelectedRec(null)}
        onApply={async () => {
          try {
            const r = await applyRecommendation(selectedRec.recommendationId);
            // Target-based redesign: text/voice apply just writes the
            // __prompt_*__ DDB row. tool_desc still creates a bundle.
            if (r.appliedBundleVersionId) {
              setSuccess(t('optimization.applyMessageToolDesc')
                .replace('{version}', r.appliedBundleVersionId));
            } else {
              setSuccess(t('optimization.applyMessageTextOnly'));
            }
            setSelectedRec(null);
            loadAll();
          } catch (e: any) { setError(e.message); }
        }}
      />}
    </SpaceBetween>
  );
};

interface GenerateRecommendationModalProps {
  scope: string;
  agentType: OptUiAgentType;
  onClose: () => void;
  onSubmitted: () => void;
  setError: (m: string) => void;
}

const GenerateRecommendationModal: React.FC<GenerateRecommendationModalProps> = ({
  scope, agentType, onClose, onSubmitted, setError,
}) => {
  const { t } = useI18n();
  const [evaluator, setEvaluator] = useState(EVALUATORS[0].value);
  const now = new Date();
  const sevenDaysAgo = new Date(now.getTime() - 7 * 86400000);
  const [startTime, setStartTime] = useState(sevenDaysAgo.toISOString());
  const [endTime, setEndTime] = useState(now.toISOString());
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    setSubmitting(true);
    try {
      // Lambda resolves the log group ARN from STS account + AWS_REGION
      // (`aws/spans` is the well-known span log group), so we omit it.
      await startRecommendation({
        scope, agentType, evaluatorArn: evaluator, startTime, endTime,
      });
      onSubmitted();
    } catch (e: any) { setError(e.message); }
    finally { setSubmitting(false); }
  };

  return (
    <Modal visible header={t('optimization.generate')} onDismiss={onClose}>
      <SpaceBetween size="m">
        <FormField label={t('optimization.evaluator')}>
          <Select
            selectedOption={EVALUATORS.find((e) => e.value === evaluator) || EVALUATORS[0]}
            options={EVALUATORS}
            onChange={({ detail }) => setEvaluator(detail.selectedOption.value as string)}
          />
        </FormField>
        <FormField label={t('optimization.startTime')}>
          <Input
            value={startTime.slice(0, 16)}
            onChange={({ detail }) => setStartTime(new Date(detail.value).toISOString())}
            type="text"
          />
        </FormField>
        <FormField label={t('optimization.endTime')}>
          <Input
            value={endTime.slice(0, 16)}
            onChange={({ detail }) => setEndTime(new Date(detail.value).toISOString())}
            type="text"
          />
        </FormField>
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="primary" loading={submitting} onClick={submit}>{t('optimization.submit')}</Button>
          <Button onClick={onClose}>{t('optimization.cancel')}</Button>
        </SpaceBetween>
      </SpaceBetween>
    </Modal>
  );
};

interface StartABTestModalProps {
  onClose: () => void;
  onSubmitted: () => void;
  setError: (m: string) => void;
}

const StartABTestModal: React.FC<StartABTestModalProps> = ({ onClose, onSubmitted, setError }) => {
  const { t } = useI18n();
  // Target-based A/B routing — variants reference runtime endpoint
  // qualifiers via gateway targets. setup-agentcore.py provisions the
  // two endpoints `control` and `treatment` at deploy time; admins can
  // repoint `treatment` to a different runtime version via the
  // `agentcore add runtime-endpoint` CLI to compare versions.
  const ENDPOINTS = [
    { value: 'control', label: 'control' },
    { value: 'treatment', label: 'treatment' },
  ];
  const [controlEp, setControlEp] = useState('control');
  const [treatmentEp, setTreatmentEp] = useState('treatment');
  const [split, setSplit] = useState<'50-50' | '80-20' | '90-10'>('50-50');
  const [duration, setDuration] = useState<1 | 3 | 7 | 14>(1);
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (controlEp === treatmentEp) {
      setError(t('optimization.startAB.sameEndpointError'));
      return;
    }
    setSubmitting(true);
    try {
      const weights = split === '50-50' ? { control: 50, treatment: 50 }
                    : split === '80-20' ? { control: 80, treatment: 20 }
                    : { control: 90, treatment: 10 };
      await startABTest({
        agentType: 'text',
        controlEndpoint: controlEp,
        treatmentEndpoint: treatmentEp,
        variantWeights: weights,
        durationDays: duration,
      });
      onSubmitted();
    } catch (e: any) { setError(e.message); }
    finally { setSubmitting(false); }
  };

  return (
    <Modal visible header={t('optimization.startAB')} onDismiss={onClose}>
      <SpaceBetween size="m">
        <FormField label={t('optimization.startAB.controlEndpoint')}>
          <Select
            selectedOption={ENDPOINTS.find((o) => o.value === controlEp) ?? ENDPOINTS[0]}
            options={ENDPOINTS}
            onChange={({ detail }) => setControlEp(detail.selectedOption.value as string)}
          />
        </FormField>
        <FormField label={t('optimization.startAB.treatmentEndpoint')}>
          <Select
            selectedOption={ENDPOINTS.find((o) => o.value === treatmentEp) ?? ENDPOINTS[1]}
            options={ENDPOINTS}
            onChange={({ detail }) => setTreatmentEp(detail.selectedOption.value as string)}
          />
        </FormField>
        <FormField label={t('optimization.split')}>
          <SegmentedControl
            selectedId={split}
            options={[
              { id: '50-50', text: '50/50' },
              { id: '80-20', text: '80/20' },
              { id: '90-10', text: '90/10 canary' },
            ]}
            onChange={({ detail }) => setSplit(detail.selectedId as '50-50' | '80-20' | '90-10')}
          />
        </FormField>
        <FormField label={t('optimization.duration')}>
          <Select
            selectedOption={{ value: String(duration), label: `${duration}d` }}
            options={[1, 3, 7, 14].map((d) => ({ value: String(d), label: `${d}d` }))}
            onChange={({ detail }) => setDuration(Number(detail.selectedOption.value) as 1 | 3 | 7 | 14)}
          />
        </FormField>
        <Alert type="info">{t('optimization.startAB.evaluatorsNote')}</Alert>
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="primary" loading={submitting} onClick={submit}
                  disabled={!controlEp || !treatmentEp || controlEp === treatmentEp}>
            {t('optimization.submit')}
          </Button>
          <Button onClick={onClose}>{t('optimization.cancel')}</Button>
        </SpaceBetween>
      </SpaceBetween>
    </Modal>
  );
};

interface RecommendationDetailDrawerProps {
  rec: OptRecommendation;
  onClose: () => void;
  onApply: () => void;
}

const RecommendationDetailDrawer: React.FC<RecommendationDetailDrawerProps> = ({ rec, onClose, onApply }) => {
  const { t } = useI18n();
  return (
    <Modal visible size="large" header={`${t('optimization.recDetail')} — ${rec.recommendationId}`} onDismiss={onClose}>
      <SpaceBetween size="m">
        <CloudscapeBox>
          <strong>Agent:</strong> {rec.agentType} · <strong>Status:</strong> {rec.status}
        </CloudscapeBox>
        {rec.recommendedSystemPrompt && (
          <FormField label={t('optimization.recommendedPrompt')}>
            <pre style={{ background: '#151515', color: '#eaeaea', padding: 12, maxHeight: 360, overflow: 'auto' }}>
              {rec.recommendedSystemPrompt}
            </pre>
          </FormField>
        )}
        {rec.tools && rec.tools.length > 0 && (
          <Table
            items={rec.tools}
            columnDefinitions={[
              { id: 'name', header: 'Tool', cell: (i: { toolName: string; recommendedToolDescription: string }) => i.toolName },
              { id: 'desc', header: 'Recommended description', cell: (i: { toolName: string; recommendedToolDescription: string }) => i.recommendedToolDescription },
            ]}
          />
        )}
        {rec.errorMessage && <Alert type="error">{rec.errorMessage}</Alert>}
        <SpaceBetween direction="horizontal" size="xs">
          {rec.status === 'COMPLETED' && <Button variant="primary" onClick={onApply}>{t('optimization.apply')}</Button>}
          <Button onClick={onClose}>{t('optimization.close')}</Button>
        </SpaceBetween>
      </SpaceBetween>
    </Modal>
  );
};

// ---------------------------------------------------------------------------
// Main AdminConsole component
// ---------------------------------------------------------------------------
interface AdminConsoleProps {
  activeTab: ActiveTab;
  setActiveTab: (t: ActiveTab) => void;
  /** Drives chart colours — the dashboard palette has separate validated
   *  steps per mode, so charts must re-colour with the theme toggle. */
  theme: 'light' | 'dark';
}
const AdminConsole: React.FC<AdminConsoleProps> = ({ activeTab, setActiveTab, theme }) => {
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [userIds, setUserIds] = useState<string[]>(['__global__']);
  const [selectedUserId, setSelectedUserId] = useState('__global__');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [currentEmail, setCurrentEmail] = useState('');
  const [showAddUserModal, setShowAddUserModal] = useState(false);
  const [newUserEmail, setNewUserEmail] = useState('');
  const [addingUser, setAddingUser] = useState(false);
  const [promotingUser, setPromotingUser] = useState('');
  const [demotingUser, setDemotingUser] = useState('');
  const [deletingUser, setDeletingUser] = useState('');
  const [deleteUserTarget, setDeleteUserTarget] = useState<{ username: string; email: string } | null>(null);
  const [createdUser, setCreatedUser] = useState<{ email: string; password: string } | null>(null);
  const { t } = useI18n();

  useEffect(() => {
    getCurrentUserEmail().then(setCurrentEmail).catch(() => setCurrentEmail(''));
  }, []);

  useEffect(() => {
    if (activeTab === 'overview' || activeTab === 'identity') {
      loadCognitoUsers();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  const handleCreateUser = async () => {
    const email = newUserEmail.trim();
    if (!email) { setError(t('overview.addUserEmailLabel')); return; }
    clearMessages();
    setAddingUser(true);
    try {
      const created = await createCognitoUser(email);
      setShowAddUserModal(false);
      setNewUserEmail('');
      setCreatedUser({ email: created.email, password: created.password });
      await loadCognitoUsers();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setAddingUser(false);
    }
  };

  const handleMakeAdmin = async (username: string, email: string) => {
    clearMessages();
    setPromotingUser(username);
    try {
      await addUserToAdminGroup(username);
      setSuccess(t('overview.userPromoted').replace('{email}', email || username));
      await loadCognitoUsers();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setPromotingUser('');
    }
  };

  const handleRemoveAdmin = async (username: string, email: string) => {
    clearMessages();
    setDemotingUser(username);
    try {
      await removeUserFromAdminGroup(username);
      setSuccess(t('overview.userDemoted').replace('{email}', email || username));
      await loadCognitoUsers();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setDemotingUser('');
    }
  };

  const handleDeleteUser = async () => {
    if (!deleteUserTarget) return;
    const { username, email } = deleteUserTarget;
    clearMessages();
    setDeletingUser(username);
    try {
      await deleteCognitoUser(username);
      setSuccess(t('overview.userDeleted').replace('{email}', email || username));
      setDeleteUserTarget(null);
      await loadCognitoUsers();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setDeletingUser('');
    }
  };

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
  // Skills awaiting a decision, and the recordId currently being reviewed so the
  // right row shows a spinner rather than the whole table.
  const [pendingRecords, setPendingRecords] = useState<RegistryRecord[]>([]);
  const [reviewing, setReviewing] = useState('');
  const [rejectReason, setRejectReason] = useState<Record<string, string>>({});

  // User settings (model ID)
  const [modelId, setModelId] = useState('');
  const [savedModelId, setSavedModelId] = useState('');

  // Active tab is hoisted to App.tsx so the SideNavigation can drive it.

  // Sessions
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [shellTarget, setShellTarget] = useState<ShellTarget | null>(null);

  // Integration Registry
  const [integrationsSubTab, setIntegrationsSubTab] =
    useState<'overview' | 'a2a' | 'skills'>('overview');
  // Approved SKILL records. Separate from the Build -> Skills page, which shows what
  // is running; this shows what the registry has approved and who imported it.
  const [registrySkills, setRegistrySkills] = useState<RegistrySkill[]>([]);
  const [registrySkillsLoading, setRegistrySkillsLoading] = useState(false);
  const [registrySkillsError, setRegistrySkillsError] = useState('');
  const [skillDrawer, setSkillDrawer] = useState<RegistrySkill | null>(null);
  const [a2aAgents, setA2aAgents] = useState<A2AAgentRecord[]>([]);
  // Keyed by recordId and fetched separately from the list: the check reads a
  // runtime per record, and the inventory should render without waiting for it.
  const [a2aConformance, setA2aConformance] =
    useState<Record<string, A2AConformanceRow>>({});
  const [a2aConformanceError, setA2aConformanceError] = useState('');
  const [a2aLoading, setA2aLoading] = useState(false);
  const [a2aError, setA2aError] = useState<string>('');
  // The platform manifest an A2A team needs. Loaded on demand rather than with the
  // page: it is a copy-once artefact, and paying a Registry round trip on every visit
  // to the inventory for something almost nobody opens is the wrong trade.
  const [a2aManifest, setA2aManifest] = useState<A2aManifest | null>(null);
  const [a2aManifestOpen, setA2aManifestOpen] = useState(false);
  const [a2aManifestLoading, setA2aManifestLoading] = useState(false);
  const [a2aManifestError, setA2aManifestError] = useState('');
  const [a2aManifestCopied, setA2aManifestCopied] = useState(false);

  const [a2aDrawer, setA2aDrawer] = useState<A2AAgentRecord | null>(null);
  // Read-only "Access" section in the Integration Registry A2A drawer.
  const [a2aDrawerGrants, setA2aDrawerGrants] = useState<A2AGrantSummary[] | null>(null);
  const [a2aDrawerGrantsLoading, setA2aDrawerGrantsLoading] = useState(false);
  // Status changes on AGENT records. Until now this console could only review SKILL
  // records — `list_registry_records` filters on recordType=SKILL — so the one status
  // transition that decides whether the orchestrator can delegate at all had to be
  // done in the AWS console or over the API.
  const [a2aReviewing, setA2aReviewing] = useState('');
  // A blocked approval, held so the admin can read the findings and decide. Not a
  // window.confirm: overriding an authorization check is a decision that needs the
  // findings visible, and `open` versus `closed` changes what overriding costs.
  const [a2aGateBlock, setA2aGateBlock] = useState<{
    record: A2AAgentRecord; conformance: A2AConformanceGate; hint: string;
  } | null>(null);
  // A rejection in progress, waiting on its reason. The API refuses a rejection with
  // no reason and it is right to: `statusReason` is the only feedback the agent's team
  // ever sees, so an unexplained rejection is indistinguishable from the platform
  // losing their agent.
  const [a2aRejecting, setA2aRejecting] = useState<
    { record: A2AAgentRecord; reason: string } | null>(null);

  // Users tab
  const [cognitoUsers, setCognitoUsers] = useState<CognitoUserInfo[]>([]);
  // Where each user is, keyed by Cognito sub. Timezone decides when a
  // time-triggered scene actually fires; the coordinates are what make a
  // sunrise/sunset trigger computable. Edited in a modal off the Identity table
  // rather than inline, because the three fields are only valid together.
  const [userPlaces, setUserPlaces] = useState<Record<string, UserPlace>>({});
  const [placeUser, setPlaceUser] = useState<CognitoUserInfo | null>(null);
  const [placeDraft, setPlaceDraft] = useState<PlaceDraft>(EMPTY_PLACE_DRAFT);
  const [savingPlace, setSavingPlace] = useState(false);
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
      // Location settings, keyed by sub for the table and written back under the
      // DDB row key (email) the agent actually reads. Fetched per user rather
      // than in one call because /settings is per-user; the list is small and a
      // failure on one row must not blank the others.
      const places: Record<string, UserPlace> = {};
      await Promise.all(users.map(async (u) => {
        try {
          const s = await getSettings(u.email || u.username || u.sub);
          places[u.sub] = {
            timezone: s.timezone || '',
            latitude: s.latitude ?? null,
            longitude: s.longitude ?? null,
          };
        } catch {
          places[u.sub] = { timezone: '', latitude: null, longitude: null };
        }
      }));
      setUserPlaces(places);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setUsersLoading(false);
    }
  }, []);

  const openPlaceEditor = (user: CognitoUserInfo) => {
    clearMessages();
    const place = userPlaces[user.sub] || { timezone: '', latitude: null, longitude: null };
    setPlaceDraft({
      timezone: place.timezone,
      latitude: place.latitude === null ? '' : String(place.latitude),
      longitude: place.longitude === null ? '' : String(place.longitude),
    });
    setPlaceUser(user);
  };

  const handleSavePlace = async () => {
    if (!placeUser) return;
    const lat = placeDraft.latitude.trim();
    const lon = placeDraft.longitude.trim();
    // Checked here as well as server-side so the admin sees it next to the field
    // they typed in rather than as an API error banner. The server check is the
    // authoritative one.
    if ((lat === '') !== (lon === '')) {
      setError(t('identity.placeBothCoords'));
      return;
    }
    clearMessages();
    setSavingPlace(true);
    const userId = placeUser.email || placeUser.username || placeUser.sub;
    try {
      await updateSettings(userId, {
        timezone: placeDraft.timezone.trim(),
        latitude: lat === '' ? null : Number(lat),
        longitude: lon === '' ? null : Number(lon),
      });
      setUserPlaces((prev) => ({
        ...prev,
        [placeUser.sub]: {
          timezone: placeDraft.timezone.trim(),
          latitude: lat === '' ? null : Number(lat),
          longitude: lon === '' ? null : Number(lon),
        },
      }));
      setSuccess(t('identity.placeSaved').replace('{user}', placeUser.email || placeUser.username || ''));
      setPlaceUser(null);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSavingPlace(false);
    }
  };

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
    setError('');
    setSuccess('');
  }, [activeTab, loadSessions, loadCognitoUsers, loadUserIds, loadSettings]);

  // Load the read-only "Access" summary whenever the A2A drawer opens.
  useEffect(() => {
    if (!a2aDrawer) {
      setA2aDrawerGrants(null);
      return;
    }
    let cancelled = false;
    setA2aDrawerGrantsLoading(true);
    listA2aGrantsForRecord(a2aDrawer.recordId)
      .then((grants) => {
        if (!cancelled) setA2aDrawerGrants(grants);
      })
      .catch((err) => {
        console.warn('Failed to load A2A grants:', err);
        if (!cancelled) setA2aDrawerGrants([]);
      })
      .finally(() => {
        if (!cancelled) setA2aDrawerGrantsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [a2aDrawer]);

  /** The A2A inventory and its conformance verdicts, as one refresh.
   *
   *  Two calls rather than one because they answer different questions and fail
   *  independently: the Registry says what we OFFER, and the conformance sweep reads
   *  each agent's own Runtime authorizer to say who could actually reach it. A
   *  throttled control plane must cost the Authorizer column, not the page.
   *
   *  Declared above the effect that depends on it: a `const` in a dependency array is
   *  read during render, so a later declaration is a TDZ ReferenceError, not a hoist.
   */
  const loadA2aInventory = useCallback(async () => {
    setA2aLoading(true);
    setA2aError('');
    checkA2aConformance()
      .then((rows) => {
        setA2aConformanceError('');
        setA2aConformance(Object.fromEntries(rows.map((r) => [r.recordId, r])));
      })
      .catch((err) => setA2aConformanceError(err.message));
    try {
      setA2aAgents(await listA2aAgents());
    } catch (err: any) {
      setA2aError(err.message);
    } finally {
      setA2aLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeTab === 'integrations' && integrationsSubTab === 'skills') {
      setRegistrySkillsLoading(true);
      setRegistrySkillsError('');
      listRegistrySkills()
        .then(({ skills, catalogError }) => {
          setRegistrySkills(skills);
          setRegistrySkillsError(catalogError);
        })
        .catch((err) => setRegistrySkillsError(err.message))
        .finally(() => setRegistrySkillsLoading(false));
    }
    if (activeTab === 'integrations' && integrationsSubTab === 'a2a') {
      void loadA2aInventory();
    }
  }, [activeTab, integrationsSubTab, loadA2aInventory]);

  const clearMessages = () => {
    setError('');
    setSuccess('');
  };

  /** Approve or reject one AGENT record.
   *
   *  `force` only ever arrives from the gate modal, never from the table: an override
   *  of an authorization check should cost a second, deliberate click on a screen that
   *  shows what is being overridden.
   *
   *  `deprecate` is deliberately not offered, though the API supports it. It is
   *  TERMINAL — the record then vanishes from the Registry API entirely, recovery means
   *  a new record with a NEW recordId, and grants are keyed on recordId, so every
   *  user's grant on that agent is silently voided. `reject` takes an agent out of
   *  service reversibly, which is what this page needs. */
  const handleA2aReview = async (
    record: A2AAgentRecord,
    decision: 'approve' | 'reject',
    force = false,
    reason = '',
  ) => {
    clearMessages();
    setA2aReviewing(record.recordId);
    try {
      const out = await reviewRegistryRecord(record.recordId, decision, reason, force);
      setA2aGateBlock(null);
      setA2aRejecting(null);
      setSuccess(t('integrations.a2a.review.done')
        .replace('{name}', record.name)
        .replace('{status}', out.status));
      await loadA2aInventory();
    } catch (err: any) {
      if (err instanceof A2AConformanceBlocked) {
        // Not an error message — a decision to put in front of the admin.
        setA2aGateBlock({ record, conformance: err.conformance, hint: err.hint });
      } else {
        setError(err.message);
      }
    } finally {
      setA2aReviewing('');
    }
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
      // Two lists: what can be imported, and what is waiting on a decision.
      // Loaded together because a reviewer opening this modal is usually here to
      // do both, and the pending queue was previously invisible in the product —
      // a skill published from the Skill ERP sat in PENDING_APPROVAL with only
      // the AWS console able to move it.
      // REJECTED is in the queue too, not only PENDING_APPROVAL. The Registry
      // allows REJECTED -> APPROVED (measured), so a reviewer who changes their
      // mind should not have to ask the author to republish — and a rejected
      // record that vanished from every screen was effectively unrecoverable
      // without the AWS console.
      const [records, pending, rejected] = await Promise.all([
        listRegistryRecords('APPROVED'),
        listRegistryRecords('PENDING_APPROVAL').catch(() => []),
        listRegistryRecords('REJECTED').catch(() => []),
      ]);
      setRegistryRecords(records);
      setPendingRecords([...pending, ...rejected]);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setRegistryLoading(false);
    }
  };

  const handleReview = async (
    recordId: string, decision: 'approve' | 'reject' | 'deprecate') => {
    clearMessages();
    const reason = (rejectReason[recordId] || '').trim();
    if (decision === 'reject' && !reason) {
      // Enforced server-side too; asking here saves a round trip and says why.
      setError(t('registry.reviewReasonRequired'));
      return;
    }
    // Deprecation is IRREVERSIBLE. Measured against the live Registry on
    // 2026-08-15: DEPRECATED is a terminal status and every transition out of it,
    // including back to APPROVED, fails with "Cannot update registry record in
    // DEPRECATED status". Recovering means recreating the record, which mints a new
    // recordId — and `__a2a_permissions__` keys grants by recordId, so every grant
    // on that agent is void until an admin repoints them. Worth one confirmation.
    if (decision === 'deprecate' &&
        !window.confirm(t('registry.deprecateIrreversible'))) {
      return;
    }
    setReviewing(recordId);
    try {
      const out = await reviewRegistryRecord(recordId, decision, reason);
      setSuccess(t('registry.reviewDone')
        .replace('{status}', out.status)
        .replace('{by}', out.reviewedBy || ''));
      // Reload both lists: an approval moves a record from one to the other.
      const [approved, pending, rejected] = await Promise.all([
        listRegistryRecords('APPROVED'),
        listRegistryRecords('PENDING_APPROVAL').catch(() => []),
        listRegistryRecords('REJECTED').catch(() => []),
      ]);
      setRegistryRecords(approved);
      setPendingRecords([...pending, ...rejected]);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setReviewing('');
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

  return (
    <div className="admin-console">
      {/* Agents (fleet + per-agent detail). A self-contained component rather
          than another block in this 4000-line file: the tab needs its own data
          loading, and every existing tab's state already lives in one shared
          component. It owns the list/detail switch internally — see AgentsPage. */}
      {activeTab === 'agents' && <AgentsPage />}
      {activeTab === 'overview' && (
        <SpaceBetween size="l">
          {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
          {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}
          <Container
            header={
              <CloudscapeHeader variant="h1" description={t('overview.desc')}>
                {t('overview.title')}
              </CloudscapeHeader>
            }
          >
            <SpaceBetween size="m">
              <CloudscapeBox variant="p">{t('overview.intro')}</CloudscapeBox>
              {/* Collapsed by default: the architecture diagram is tall, and when
                  demoing to an administrator the operational metrics below are
                  what should be on screen first. */}
              <ExpandableSection
                variant="footer"
                headerText={t('overview.diagramToggle')}
              >
                <img
                  src={architectureDiagram}
                  alt={t('overview.diagramAlt')}
                  style={{ maxWidth: '100%', height: 'auto', display: 'block' }}
                />
              </ExpandableSection>
            </SpaceBetween>
          </Container>
          {/* Agent ops dashboard — spec 2026-07-29. User management moved to
              Build > Identity and the demo launchers moved to the side nav, so
              Overview is intro + architecture + operational metrics. */}
          <DashboardSection theme={theme} />
        </SpaceBetween>
      )}

      {activeTab === 'identity' && (
        <SpaceBetween size="l">
        {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
        {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}
        <Table
          header={
            <CloudscapeHeader
              variant="h2"
              description={t('identity.desc')}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" onClick={() => loadCognitoUsers()}>
                    {t('overview.refresh')}
                  </Button>
                  <Button variant="primary"
                          onClick={() => { setNewUserEmail(''); setShowAddUserModal(true); }}>
                    {t('overview.addUser')}
                  </Button>
                </SpaceBetween>
              }
            >
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
              cell: (u) => <GroupsCell groups={u.groups} t={t} />,
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
            {
              // Timezone and coordinates. Read-only here with an Edit button —
              // "UTC" shown for an unset zone is the truth, not a placeholder:
              // that is the zone their scenes are actually scheduled in.
              id: 'place',
              header: t('identity.colPlace'),
              minWidth: 200,
              cell: (u) => {
                const place = userPlaces[u.sub];
                const zone = place?.timezone || 'UTC';
                const coords = place && place.latitude !== null && place.longitude !== null
                  ? `${place.latitude.toFixed(2)}, ${place.longitude.toFixed(2)}`
                  : t('identity.placeNoCoords');
                return (
                  <SpaceBetween direction="horizontal" size="xs">
                    <span>{zone}</span>
                    <span style={{ opacity: 0.7 }}>({coords})</span>
                    <Button variant="inline-link" onClick={() => openPlaceEditor(u)}>
                      {t('identity.placeEdit')}
                    </Button>
                  </SpaceBetween>
                );
              },
            },
            {
              // Promote/demote/delete moved here from Overview (spec 2026-07-29):
              // user management now lives entirely under Build > Identity.
              id: 'actions',
              header: t('overview.colActions'),
              minWidth: 280,
              cell: (u) => {
                const isAdmin = (u.groups || []).includes('admin');
                const isSelf = u.email && currentEmail && u.email.toLowerCase() === currentEmail.toLowerCase();
                return (
                  <SpaceBetween direction="horizontal" size="xxs">
                    {isAdmin ? (
                      <Button
                        disabled={!!isSelf}
                        loading={demotingUser === u.username}
                        onClick={() => handleRemoveAdmin(u.username, u.email)}
                      >
                        {t('overview.removeAdmin')}
                      </Button>
                    ) : (
                      <Button
                        loading={promotingUser === u.username}
                        onClick={() => handleMakeAdmin(u.username, u.email)}
                      >
                        {t('overview.makeAdmin')}
                      </Button>
                    )}
                    <Button
                      disabled={!!isSelf}
                      loading={deletingUser === u.username}
                      onClick={() => setDeleteUserTarget({ username: u.username, email: u.email })}
                    >
                      {t('overview.deleteUser')}
                    </Button>
                  </SpaceBetween>
                );
              },
            },
          ]}
          empty={<CloudscapeBox textAlign="center" padding="m"><b>{t('users.noUsers')}</b></CloudscapeBox>}
        />
        <Modal
          visible={!!deleteUserTarget}
          onDismiss={() => setDeleteUserTarget(null)}
          header={t('overview.deleteConfirmTitle')}
          footer={
            <CloudscapeBox float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setDeleteUserTarget(null)}>
                  {t('overview.cancel')}
                </Button>
                <Button variant="primary" loading={!!deletingUser} onClick={handleDeleteUser}>
                  {t('overview.deleteUser')}
                </Button>
              </SpaceBetween>
            </CloudscapeBox>
          }
        >
          <CloudscapeBox variant="p">
            {t('overview.deleteConfirmBody').replace('{email}', deleteUserTarget?.email || deleteUserTarget?.username || '')}
          </CloudscapeBox>
        </Modal>
        <Modal
          visible={!!placeUser}
          onDismiss={() => setPlaceUser(null)}
          header={t('identity.placeTitle').replace('{user}', placeUser?.email || placeUser?.username || '')}
          footer={
            <CloudscapeBox float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setPlaceUser(null)}>
                  {t('overview.cancel')}
                </Button>
                <Button variant="primary" loading={savingPlace} onClick={handleSavePlace}>
                  {t('models.save')}
                </Button>
              </SpaceBetween>
            </CloudscapeBox>
          }
        >
          <SpaceBetween size="m">
            <FormField
              label={t('identity.placeTimezone')}
              description={t('identity.placeTimezoneHint')}
            >
              <Autosuggest
                value={placeDraft.timezone}
                onChange={({ detail }) => setPlaceDraft((p) => ({ ...p, timezone: detail.value }))}
                options={COMMON_TIMEZONES.map((tz) => ({ value: tz }))}
                enteredTextLabel={(v) => v}
                placeholder="UTC"
              />
            </FormField>
            <FormField
              label={t('identity.placeCoords')}
              description={t('identity.placeCoordsHint')}
            >
              <SpaceBetween direction="horizontal" size="xs">
                <Input
                  value={placeDraft.latitude}
                  onChange={({ detail }) => setPlaceDraft((p) => ({ ...p, latitude: detail.value }))}
                  placeholder={t('identity.placeLatitude')}
                  type="number"
                  inputMode="decimal"
                />
                <Input
                  value={placeDraft.longitude}
                  onChange={({ detail }) => setPlaceDraft((p) => ({ ...p, longitude: detail.value }))}
                  placeholder={t('identity.placeLongitude')}
                  type="number"
                  inputMode="decimal"
                />
              </SpaceBetween>
            </FormField>
          </SpaceBetween>
        </Modal>
        </SpaceBetween>
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
      {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}

      <SpaceBetween size="s" direction="horizontal">
        <span style={{ alignSelf: 'center' }}>{t('skills.userScope')}</span>
        <div style={{ minWidth: 220 }}>
          <Select
            selectedOption={selectedUserOption}
            onChange={({ detail }) => setSelectedUserId(detail.selectedOption.value as string)}
            options={userScopeOptions}
          />
        </div>
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
            {/* The review queue. Placed above the import list because a skill has
                to be approved before it can be imported, so this is the first
                thing a reviewer needs — and because the queue was invisible in the
                product until now: the Registry holds the state machine and nothing
                here called it. */}
            {pendingRecords.length > 0 && (
              <Container header={
                <CloudscapeHeader variant="h3" description={t('registry.reviewDesc')}
                                  counter={`(${pendingRecords.length})`}>
                  {t('registry.reviewTitle')}
                </CloudscapeHeader>
              }>
                <Table
                  variant="embedded"
                  contentDensity="compact"
                  items={pendingRecords}
                  trackBy="recordId"
                  columnDefinitions={[
                    { id: 'name', header: t('registry.colName'), cell: (r) => r.name },
                    {
                      id: 'status',
                      header: t('registry.colStatus'),
                      cell: (r) => (r.status === 'REJECTED'
                        ? <StatusIndicator type="error">{r.status}</StatusIndicator>
                        : <StatusIndicator type="pending">{r.status}</StatusIndicator>),
                    },
                    { id: 'description', header: t('registry.colDescription'),
                      cell: (r) => r.description },
                    {
                      id: 'reason',
                      header: t('registry.reviewReason'),
                      minWidth: 200,
                      // Required to reject: statusReason is the only feedback the
                      // skill's author ever sees, so an unexplained rejection is
                      // indistinguishable from the system losing their work.
                      cell: (r) => (
                        <Input
                          value={rejectReason[r.recordId] || ''}
                          placeholder={t('registry.reviewReasonPlaceholder')}
                          onChange={({ detail }) =>
                            setRejectReason((prev) => ({ ...prev, [r.recordId]: detail.value }))}
                        />
                      ),
                    },
                    {
                      id: 'actions',
                      header: t('registry.colActions'),
                      minWidth: 210,
                      cell: (r) => (
                        <SpaceBetween direction="horizontal" size="xxs">
                          <Button variant="primary"
                                  loading={reviewing === r.recordId}
                                  onClick={() => handleReview(r.recordId, 'approve')}>
                            {t('registry.approve')}
                          </Button>
                          {r.status !== 'REJECTED' && (
                            <Button loading={reviewing === r.recordId}
                                    onClick={() => handleReview(r.recordId, 'reject')}>
                              {t('registry.reject')}
                            </Button>
                          )}
                        </SpaceBetween>
                      ),
                    },
                  ]}
                />
              </Container>
            )}
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

      {/* Scenarios Tab */}
      {activeTab === 'scenarios' && (
        <ScenariosTab
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

      {activeTab === 'optimization' && (
        <OptimizationTab
          error={error}
          success={success}
          setError={setError}
          setSuccess={setSuccess}
          cognitoUsers={cognitoUsers}
        />
      )}

      {/* Sessions Tab */}
      {activeTab === 'sessions' && (
        <SpaceBetween size="l">
          {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
          {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}

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
                // The agent is named next to the number when the split says which
                // one it was. Until now every session's tokens were one figure
                // with no owner, which is unreadable once nine runtimes report
                // into the same log group.
                cell: (s) => {
                  if (typeof s.totalTokens7d !== 'number') return '-';
                  const split = Object.entries(s.tokensByAgent || {});
                  return (
                    <span title={split.map(([a, n]) => `${a}: ${n.toLocaleString()}`).join('\n')}>
                      {s.totalTokens7d.toLocaleString()}
                      {split.length === 1 && (
                        <CloudscapeBox variant="small" color="text-body-secondary" display="inline">
                          {` ${split[0][0]}`}
                        </CloudscapeBox>
                      )}
                      {split.length > 1 && (
                        <CloudscapeBox variant="small" color="text-body-secondary" display="inline">
                          {` ${split.length} agents`}
                        </CloudscapeBox>
                      )}
                    </span>
                  );
                },
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

      {/* SubAgent Policy Tab — its own component; AdminConsole.tsx was already
          4700+ lines and an A2A grant is a different object from a gateway tool. */}
      {activeTab === 'subAgentPolicy' && (
        <SubAgentPolicyPage
          error={error}
          success={success}
          clearMessages={clearMessages}
          setError={setError}
          setSuccess={setSuccess}
        />
      )}

      {/* Tool Access Tab */}
      {activeTab === 'users' && (
        <SpaceBetween size="l">
          {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
          {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}

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
                          {/* Who breaks if this is revoked. The list was flat
                              before, so nothing said that turning off
                              control_device also stops the scheduled scenes and
                              two specialists. */}
                          {!!tool.consumers?.length && (
                            <span className="perm-tool-consumers"
                                  title={t('users.toolConsumersHint')}>
                              {tool.consumers.join(', ')}
                            </span>
                          )}
                        </label>
                      ))}
                    </div>
                  </>
                )}

                {/* The A2A grant section used to live here. It moved to
                    Build -> SubAgent Policy on 2026-08-12: it had no way to express
                    a global default (this panel is keyed on one selected user), and
                    an A2A grant is now a Cognito group checked by the sub-agent
                    itself rather than a Cedar policy on this gateway. Deliberately
                    NOT duplicated here — two write paths to one grant is how they
                    end up disagreeing. */}
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
                  cell: (u) => <GroupsCell groups={u.groups} t={t} />,
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
            onChange={({ detail }) =>
              setIntegrationsSubTab(detail.selectedId as 'overview' | 'a2a' | 'skills')}
            options={[
              { id: 'overview', text: t('integrations.sub.overview') },
              { id: 'a2a', text: t('integrations.sub.a2a') },
              { id: 'skills', text: t('integrations.sub.skills') },
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

          {integrationsSubTab === 'skills' && (
            <SpaceBetween size="l">
              {/* A failed registry read and an empty registry produce the same empty
                  table, and only one is worth an admin's time. */}
              {registrySkillsError && (
                <Alert type="warning" header={t('integrations.skills.loadFailedTitle')}>
                  {t('integrations.skills.loadFailed').replace('{error}', registrySkillsError)}
                </Alert>
              )}
              <Table
                header={
                  <CloudscapeHeader
                    variant="h2"
                    counter={`(${registrySkills.length})`}
                    description={t('integrations.skills.desc')}
                  >
                    {t('integrations.skills.title')}
                  </CloudscapeHeader>
                }
                loading={registrySkillsLoading}
                loadingText={t('integrations.skills.loading')}
                items={registrySkills}
                trackBy="recordId"
                columnDefinitions={[
                  { id: 'name', header: t('integrations.skills.col.name'), cell: (r) => r.name },
                  {
                    id: 'description',
                    header: t('integrations.skills.col.description'),
                    cell: (r) => r.description || '—',
                  },
                  { id: 'version', header: t('integrations.skills.col.version'), cell: (r) => r.version || '—' },
                  {
                    // Every row here is APPROVED today, because that is what the
                    // endpoint filters on. Shown anyway: if the filter is ever
                    // widened, a DRAFT record must be distinguishable from a live
                    // one, and a record that is present but unapproved is exactly
                    // the thing an admin is looking for when a skill "is missing".
                    id: 'status',
                    header: t('integrations.skills.col.status'),
                    cell: (r) => (r.status === 'APPROVED'
                      ? <StatusIndicator type="success">{r.status}</StatusIndicator>
                      : <StatusIndicator type="pending">{r.status || '—'}</StatusIndicator>),
                  },
                  {
                    id: 'publishedBy',
                    header: t('integrations.skills.col.publishedBy'),
                    cell: (r) => r.publishedBy || '—',
                  },
                  {
                    // The question an admin actually has about an approved skill.
                    id: 'importedBy',
                    header: t('integrations.skills.col.importedBy'),
                    cell: (r) => (r.importedBy.length === 0
                      ? <Badge color="grey">{t('integrations.skills.notImported')}</Badge>
                      : <span>{r.importedBy.map(displayUserId).join(', ')}</span>),
                  },
                  { id: 'license', header: t('integrations.skills.col.license'), cell: (r) => r.license || '—' },
                  {
                    id: 'updated',
                    header: t('integrations.skills.col.updated'),
                    cell: (r) => (r.updatedAt ? r.updatedAt.slice(0, 19).replace('T', ' ') : '—'),
                  },
                  {
                    id: 'actions',
                    header: t('integrations.skills.col.actions'),
                    minWidth: 100,
                    cell: (r) => <Button onClick={() => setSkillDrawer(r)}>{t('integrations.skills.view')}</Button>,
                  },
                ]}
                empty={
                  <CloudscapeBox textAlign="center" padding="m">
                    <b>{t('integrations.skills.empty')}</b>
                    <CloudscapeBox variant="p" color="text-body-secondary" padding={{ top: 'xs' }}>
                      {registrySkillsError
                        ? t('integrations.skills.emptyBecauseError')
                        : t('integrations.skills.emptyHint')}
                    </CloudscapeBox>
                  </CloudscapeBox>
                }
              />

              {skillDrawer && (
                <Modal
                  visible
                  onDismiss={() => setSkillDrawer(null)}
                  size="large"
                  header={skillDrawer.name}
                  footer={
                    <CloudscapeBox float="right">
                      <Button onClick={() => setSkillDrawer(null)}>
                        {t('integrations.skills.close')}
                      </Button>
                    </CloudscapeBox>
                  }
                >
                  <SpaceBetween size="m">
                    <p>{skillDrawer.description}</p>
                    {skillDrawer.readError && (
                      <Alert type="warning" header={t('integrations.skills.readErrorTitle')}>
                        {t('integrations.skills.readError').replace('{error}', skillDrawer.readError)}
                      </Alert>
                    )}
                    <dl className="drawer-fields">
                      <dt>{t('integrations.skills.drawer.recordId')}</dt>
                      <dd><code>{skillDrawer.recordId}</code></dd>
                      <dt>{t('integrations.skills.drawer.dedupName')}</dt>
                      <dd><code>{skillDrawer.dedupName || '—'}</code></dd>
                      <dt>{t('integrations.skills.col.version')}</dt>
                      <dd>{skillDrawer.version || '—'}</dd>
                      <dt>{t('integrations.skills.col.license')}</dt>
                      <dd>{skillDrawer.license || '—'}</dd>
                      <dt>{t('integrations.skills.drawer.compatibility')}</dt>
                      <dd>{skillDrawer.compatibility || '—'}</dd>
                      <dt>{t('integrations.skills.col.publishedBy')}</dt>
                      <dd>{skillDrawer.publishedBy || '—'}</dd>
                    </dl>

                    {/* Access: which scopes are actually running this. The mirror of
                        the A2A drawer's grants summary. */}
                    <div>
                      <b>{t('integrations.skills.drawer.access')}</b>
                      {skillDrawer.importedBy.length === 0 ? (
                        <CloudscapeBox color="text-body-secondary" padding={{ top: 'xs' }}>
                          {t('integrations.skills.drawer.noAccess')}
                        </CloudscapeBox>
                      ) : (
                        <ul>
                          {skillDrawer.importedBy.map((scope) => (
                            <li key={scope}><code>{displayUserId(scope)}</code></li>
                          ))}
                        </ul>
                      )}
                    </div>

                    <div>
                      <b>SKILL.md</b>
                      {skillDrawer.skillMd ? (
                        <pre className="skill-md-preview">{skillDrawer.skillMd}</pre>
                      ) : (
                        <CloudscapeBox color="text-body-secondary" padding={{ top: 'xs' }}>
                          {t('integrations.skills.drawer.noSkillMd')}
                        </CloudscapeBox>
                      )}
                    </div>
                  </SpaceBetween>
                </Modal>
              )}
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
                      <SpaceBetween size="xs" direction="horizontal">
                      <Button
                        iconName="external"
                        onClick={() => {
                          setA2aManifestOpen(true);
                          setA2aManifestCopied(false);
                          if (a2aManifest) return;   // cached for the session
                          setA2aManifestLoading(true);
                          setA2aManifestError('');
                          getA2aManifest()
                            .then(setA2aManifest)
                            .catch((err: any) => setA2aManifestError(err.message))
                            .finally(() => setA2aManifestLoading(false));
                        }}
                      >
                        {t('integrations.a2a.manifest.button')}
                      </Button>
                      <Button
                        iconName="refresh"
                        loading={a2aLoading}
                        onClick={() => void loadA2aInventory()}
                      >
                        {t('integrations.a2a.refresh')}
                      </Button>
                      </SpaceBetween>
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
                    // What the CARD says a caller must send. Read from A2A 0.3.0's
                    // `security` / `securitySchemes`, falling back to 0.2.x's
                    // `authentication.schemes` — reading only the latter is why this
                    // column said `none` for all eight agents: every card here is
                    // 0.3.0, where that field does not exist.
                    //
                    // Declarative, and deliberately NOT the same question as the
                    // Authorizer column. This is the agent's own claim about how to
                    // authenticate to it; the door is its Runtime authorizer.
                    id: 'auth',
                    header: t('integrations.a2a.col.auth'),
                    cell: (r) => {
                      const schemes = cardAuthSchemes(r.card);
                      if (schemes.length === 0) {
                        return (
                          <span title={t('integrations.a2a.auth.noneHint')}>
                            <StatusIndicator type="warning">
                              {t('integrations.a2a.auth.noneDeclared')}
                            </StatusIndicator>
                          </span>
                        );
                      }
                      const detail = schemes
                        .map((s) => {
                          const scheme = r.card.securitySchemes?.[s];
                          const flow = Object.keys(scheme?.flows || {})[0];
                          return flow ? `${s} (${flow})` : s;
                        })
                        .join(', ');
                      return <span title={detail}>{schemes.join(', ')}</span>;
                    },
                  },
                  {
                    // Registry status, which is what decides whether the orchestrator
                    // resolves this agent at all — it lists APPROVED records only. The
                    // column did not exist, so a record knocked back to DRAFT by a
                    // version bump was indistinguishable from a healthy one here, and
                    // the only hint was the Access column's expiry badge.
                    id: 'status',
                    header: t('integrations.a2a.col.status'),
                    cell: (r) => (
                      <SpaceBetween direction="horizontal" size="xxs">
                        <StatusIndicator
                          type={r.status === 'APPROVED' ? 'success'
                            : r.status === 'REJECTED' ? 'error'
                              : r.status === 'PENDING_APPROVAL' ? 'pending' : 'info'}
                        >
                          {r.status || '—'}
                        </StatusIndicator>
                        {r.recordVersion && <Badge>v{r.recordVersion}</Badge>}
                      </SpaceBetween>
                    ),
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
                    // Registry status and "can it be reached" are different
                    // questions and both belong here. A record edited back to DRAFT
                    // is not approved and is still reachable for the length of its
                    // re-approval window; a REJECTED one stops being reachable at
                    // once. This column is what answers "why did access to the
                    // energy specialist disappear", which is the question that
                    // brings an admin to this page.
                    id: 'access',
                    header: t('integrations.a2a.col.access'),
                    cell: (r) => {
                      const remaining = r.graceRemainingSeconds;
                      return (
                        <SpaceBetween direction="horizontal" size="xxs">
                          <StatusIndicator type={r.grantable ? 'success' : 'error'}>
                            {r.grantable
                              ? t('integrations.a2a.access.grantable')
                              : t('integrations.a2a.access.revoked')}
                          </StatusIndicator>
                          {remaining !== null && remaining !== undefined && (
                            <Badge color="severity-medium">
                              {t('integrations.a2a.access.expiresIn', {
                                minutes: Math.max(1, Math.round(remaining / 60)),
                              })}
                            </Badge>
                          )}
                        </SpaceBetween>
                      );
                    },
                  },
                  {
                    id: 'accessReason',
                    header: t('integrations.a2a.col.accessReason'),
                    cell: (r) => r.grantableReason || '—',
                  },
                  {
                    // Registry status says whether we OFFER the agent; this says
                    // whether the agent's own Runtime authorizer will accept the
                    // people we offer it to. They are independent, and a record can
                    // read `approved` while nobody can call it — or while everybody
                    // can, which is the worse case and why `open` is red.
                    id: 'authorizer',
                    header: t('integrations.a2a.col.authorizer'),
                    cell: (r) => {
                      const row = a2aConformance[r.recordId];
                      if (!row) {
                        return (
                          <StatusIndicator type="pending">
                            {t('integrations.a2a.auth.checking')}
                          </StatusIndicator>
                        );
                      }
                      if (row.conformant) {
                        return (
                          <StatusIndicator type="success">
                            {t('integrations.a2a.auth.ok')}
                          </StatusIndicator>
                        );
                      }
                      const type = row.severity === 'open' ? 'error'
                        : row.severity === 'closed' ? 'warning' : 'info';
                      const label = row.severity === 'open'
                        ? t('integrations.a2a.auth.open')
                        : row.severity === 'closed'
                          ? t('integrations.a2a.auth.closed')
                          : t('integrations.a2a.auth.unknown');
                      return (
                        <StatusIndicator type={type}>{label}</StatusIndicator>
                      );
                    },
                  },
                  {
                    id: 'lastUpdated',
                    header: t('integrations.a2a.col.lastUpdated'),
                    cell: (r) => (r.updatedAt ? new Date(r.updatedAt).toLocaleString() : '-'),
                  },
                  {
                    id: 'actions',
                    header: t('integrations.a2a.col.actions'),
                    minWidth: 240,
                    cell: (r) => (
                      <SpaceBetween direction="horizontal" size="xxs">
                        <Button onClick={() => setA2aDrawer(r)}>
                          {t('integrations.a2a.view')}
                        </Button>
                        {/* DRAFT, PENDING_APPROVAL and REJECTED can all reach
                            APPROVED (a DRAFT is submitted first, server-side). An
                            already-approved record gets no Approve button — the call
                            would be a no-op that reads as an action. */}
                        {r.status !== 'APPROVED' && (
                          <Button
                            variant="primary"
                            loading={a2aReviewing === r.recordId}
                            onClick={() => void handleA2aReview(r, 'approve')}
                          >
                            {t('integrations.a2a.review.approve')}
                          </Button>
                        )}
                        {/* A DRAFT cannot be rejected — it was never submitted for
                            review, and the API answers 409. Offering the button would
                            be offering a guaranteed error. */}
                        {r.status !== 'DRAFT' && r.status !== 'REJECTED' && (
                          <Button
                            loading={a2aReviewing === r.recordId}
                            onClick={() => setA2aRejecting({ record: r, reason: '' })}
                          >
                            {t('integrations.a2a.review.reject')}
                          </Button>
                        )}
                      </SpaceBetween>
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

              {/* The platform manifest. Rendered as ONE raw JSON block with a single
                  copy button, on purpose: an A2A team's CI consumes the whole document,
                  and a prettified field-by-field view invites copying half of it. The
                  values are public — the same discovery URL and app client id every
                  browser app already ships — so the risk here is an INCOMPLETE copy,
                  not an exposed one. */}
              {/* The approval gate refused. Shown as its own decision screen rather
                  than as an error toast, because the admin has a real choice here and
                  it is not a symmetric one: `open` means approving publishes an agent
                  that anyone in the pool can call, `closed` means it publishes one
                  nobody can. Neither raises an alarm in production — the first looks
                  like everything works — so the findings have to be readable at the
                  moment of the decision, and the override goes into the record's own
                  statusReason server-side. */}
              {/* Rejecting needs a reason, and it is worth a modal rather than a
                  prompt: it is the ONLY channel back to the agent's team, and it takes
                  the agent out of service the moment it lands — a rejected record is
                  refused immediately, with no re-approval window, and the inline sweep
                  revokes its grants on the way out. */}
              {a2aRejecting && (
                <Modal
                  visible
                  onDismiss={() => setA2aRejecting(null)}
                  header={t('integrations.a2a.review.rejectTitle')
                    .replace('{name}', a2aRejecting.record.name)}
                  footer={
                    <CloudscapeBox float="right">
                      <SpaceBetween size="xs" direction="horizontal">
                        <Button onClick={() => setA2aRejecting(null)}>
                          {t('integrations.a2a.review.cancel')}
                        </Button>
                        <Button
                          variant="primary"
                          disabled={!a2aRejecting.reason.trim()}
                          loading={a2aReviewing === a2aRejecting.record.recordId}
                          onClick={() => void handleA2aReview(
                            a2aRejecting.record, 'reject', false,
                            a2aRejecting.reason.trim())}
                        >
                          {t('integrations.a2a.review.reject')}
                        </Button>
                      </SpaceBetween>
                    </CloudscapeBox>
                  }
                >
                  <SpaceBetween size="s">
                    <CloudscapeBox variant="p">
                      {t('integrations.a2a.review.rejectHint')}
                    </CloudscapeBox>
                    <Input
                      value={a2aRejecting.reason}
                      placeholder={t('integrations.a2a.review.rejectPlaceholder')}
                      onChange={({ detail }) => setA2aRejecting(
                        { ...a2aRejecting, reason: detail.value })}
                    />
                  </SpaceBetween>
                </Modal>
              )}

              {a2aGateBlock && (
                <Modal
                  visible
                  onDismiss={() => setA2aGateBlock(null)}
                  header={t('integrations.a2a.review.blockedTitle')
                    .replace('{name}', a2aGateBlock.record.name)}
                  size="large"
                  footer={
                    <CloudscapeBox float="right">
                      <SpaceBetween size="xs" direction="horizontal">
                        <Button onClick={() => setA2aGateBlock(null)}>
                          {t('integrations.a2a.review.cancel')}
                        </Button>
                        <Button
                          loading={a2aReviewing === a2aGateBlock.record.recordId}
                          onClick={() => void handleA2aReview(
                            a2aGateBlock.record, 'approve', true)}
                        >
                          {t('integrations.a2a.review.forceApprove')}
                        </Button>
                      </SpaceBetween>
                    </CloudscapeBox>
                  }
                >
                  <SpaceBetween size="m">
                    <Alert
                      type={a2aGateBlock.conformance.severity === 'open'
                        ? 'error' : 'warning'}
                      header={a2aGateBlock.conformance.severity === 'open'
                        ? t('integrations.a2a.auth.openHeader')
                        : t('integrations.a2a.auth.closedHeader')}
                    >
                      <ul>
                        {a2aGateBlock.conformance.findings.map((f) => (
                          <li key={f.code}>
                            <strong>{f.code}</strong> — {f.detail}
                          </li>
                        ))}
                      </ul>
                    </Alert>
                    <CloudscapeBox variant="p">
                      {a2aGateBlock.hint || t('integrations.a2a.auth.fixHint')}
                    </CloudscapeBox>
                    <CloudscapeBox variant="code">
                      {'./venv/bin/python scripts/a2a-authorizer-contract.py '
                        + `--record-id ${a2aGateBlock.record.recordId}`}
                    </CloudscapeBox>
                    <CloudscapeBox variant="p" color="text-status-warning">
                      {t('integrations.a2a.review.forceWarning')}
                    </CloudscapeBox>
                  </SpaceBetween>
                </Modal>
              )}

              {a2aManifestOpen && (
                <Modal
                  visible
                  onDismiss={() => setA2aManifestOpen(false)}
                  header={t('integrations.a2a.manifest.title')}
                  size="large"
                  footer={
                    <CloudscapeBox float="right">
                      <SpaceBetween size="xs" direction="horizontal">
                        <Button
                          variant="primary"
                          iconName={a2aManifestCopied ? 'status-positive' : 'copy'}
                          disabled={!a2aManifest}
                          onClick={() => {
                            if (!a2aManifest) return;
                            navigator.clipboard
                              .writeText(JSON.stringify(a2aManifest, null, 2))
                              .then(() => setA2aManifestCopied(true))
                              .catch(() => setA2aManifestError(
                                t('integrations.a2a.manifest.copyFailed')));
                          }}
                        >
                          {a2aManifestCopied
                            ? t('integrations.a2a.manifest.copied')
                            : t('integrations.a2a.manifest.copy')}
                        </Button>
                        <Button onClick={() => setA2aManifestOpen(false)}>
                          {t('form.close')}
                        </Button>
                      </SpaceBetween>
                    </CloudscapeBox>
                  }
                >
                  <SpaceBetween size="m">
                    <CloudscapeBox variant="p" color="text-body-secondary">
                      {t('integrations.a2a.manifest.intro')}
                    </CloudscapeBox>
                    {a2aManifestError && (
                      <Alert type="error">{a2aManifestError}</Alert>
                    )}
                    {a2aManifestLoading && (
                      <StatusIndicator type="loading">
                        {t('common.loading')}
                      </StatusIndicator>
                    )}
                    {a2aManifest && (
                      <CloudscapeBox variant="code">
                        <pre style={{
                          margin: 0, maxHeight: '48vh', overflow: 'auto',
                          fontSize: '12px', whiteSpace: 'pre-wrap',
                          wordBreak: 'break-all',
                        }}>
                          {JSON.stringify(a2aManifest, null, 2)}
                        </pre>
                      </CloudscapeBox>
                    )}
                  </SpaceBetween>
                </Modal>
              )}

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
                    {/* The authorizer findings, above the card details, because a
                        non-conformant agent is the reason someone opened this drawer:
                        the card itself looks fine in every one of these cases. */}
                    {(() => {
                      const row = a2aConformance[a2aDrawer.recordId];
                      if (!row || row.conformant) return null;
                      return (
                        <Alert
                          type={row.severity === 'open' ? 'error' : 'warning'}
                          header={row.severity === 'open'
                            ? t('integrations.a2a.auth.openHeader')
                            : t('integrations.a2a.auth.closedHeader')}
                        >
                          <ul>
                            {row.findings.map((f) => (
                              <li key={f.code}>
                                <strong>{f.code}</strong> — {f.detail}
                              </li>
                            ))}
                          </ul>
                          <CloudscapeBox variant="p" padding={{ top: 'xs' }}>
                            {t('integrations.a2a.auth.fixHint')}
                          </CloudscapeBox>
                          <CloudscapeBox variant="code">
                            {`./venv/bin/python scripts/a2a-authorizer-contract.py --record-id ${a2aDrawer.recordId}`}
                          </CloudscapeBox>
                          {row.resolvedVia && (
                            <CloudscapeBox variant="p" color="text-body-secondary"
                              padding={{ top: 'xs' }}>
                              {t('integrations.a2a.auth.resolvedVia')}: {row.resolvedVia}
                            </CloudscapeBox>
                          )}
                        </Alert>
                      );
                    })()}
                    <dl className="drawer-fields">
                      {/* Record-level first, card second. The record is what the
                          platform acts on — status is what decides whether the
                          orchestrator resolves this agent at all — and it used to be
                          absent here entirely, so this panel could not answer the
                          question that brings an admin to it. */}
                      <dt>{t('integrations.a2a.drawer.status')}</dt>
                      <dd>
                        {a2aDrawer.status}
                        {a2aDrawer.recordVersion ? ` · v${a2aDrawer.recordVersion}` : ''}
                      </dd>
                      {a2aDrawer.statusReason && (
                        <>
                          <dt>{t('integrations.a2a.drawer.statusReason')}</dt>
                          <dd>{a2aDrawer.statusReason}</dd>
                        </>
                      )}
                      <dt>{t('integrations.a2a.drawer.grantable')}</dt>
                      <dd>
                        {a2aDrawer.grantable
                          ? t('integrations.a2a.access.grantable')
                          : t('integrations.a2a.access.revoked')}
                        {a2aDrawer.grantableReason ? ` — ${a2aDrawer.grantableReason}` : ''}
                      </dd>
                      <dt>{t('integrations.a2a.drawer.endpoint')}</dt>
                      <dd>{a2aDrawer.card.url}</dd>
                      <dt>{t('integrations.a2a.drawer.version')}</dt>
                      <dd>
                        {a2aDrawer.card.version}
                        {a2aDrawer.card.protocolVersion
                          ? ` · A2A ${a2aDrawer.card.protocolVersion}`
                          : ''}
                      </dd>
                      <dt>{t('integrations.a2a.drawer.provider')}</dt>
                      <dd>{a2aDrawer.card.provider?.organization || '—'}</dd>
                      <dt>{t('integrations.a2a.drawer.auth')}</dt>
                      <dd>
                        {cardAuthSchemes(a2aDrawer.card).join(', ')
                          || t('integrations.a2a.auth.noneDeclared')}
                        <CloudscapeBox variant="small" color="text-body-secondary">
                          {t('integrations.a2a.drawer.authNote')}
                        </CloudscapeBox>
                      </dd>
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

                    {/* The record verbatim. The fields above are a reading of it, and a
                        reading always lags: `securitySchemes` was rendered from a field
                        the A2A spec renamed two versions ago and quietly showed "none"
                        for every agent. Whatever this panel forgets to interpret is
                        still here, which is what makes it answerable rather than
                        merely tidy. */}
                    <ExpandableSection headerText={t('integrations.a2a.drawer.rawTitle')}>
                      <CloudscapeBox variant="p" color="text-body-secondary">
                        {t('integrations.a2a.drawer.rawHint')}
                      </CloudscapeBox>
                      <pre className="a2a-raw-record">
                        {JSON.stringify(
                          {
                            recordId: a2aDrawer.recordId,
                            name: a2aDrawer.name,
                            status: a2aDrawer.status,
                            statusReason: a2aDrawer.statusReason || '',
                            recordVersion: a2aDrawer.recordVersion || '',
                            description: a2aDrawer.description,
                            publishedBy: a2aDrawer.publishedBy,
                            grantable: a2aDrawer.grantable,
                            grantableReason: a2aDrawer.grantableReason,
                            graceRemainingSeconds: a2aDrawer.graceRemainingSeconds,
                            createdAt: a2aDrawer.createdAt,
                            updatedAt: a2aDrawer.updatedAt,
                            agentCard: a2aDrawer.card,
                          },
                          null,
                          2,
                        )}
                      </pre>
                    </ExpandableSection>

                    {/* Read-only Access section: shows who has been granted
                        which skills on this A2A agent. All writes go through
                        Users → Manage Permissions. */}
                    <div className="drawer-access-section">
                      <b>{t('integrations.a2a.access.title')}</b>
                      {a2aDrawerGrantsLoading ? (
                        <CloudscapeBox color="text-body-secondary" padding="s">
                          {t('common.loading')}
                        </CloudscapeBox>
                      ) : !a2aDrawerGrants || a2aDrawerGrants.length === 0 ? (
                        <CloudscapeBox color="text-body-secondary" padding="s">
                          {t('integrations.a2a.access.empty')}
                        </CloudscapeBox>
                      ) : (
                        <ul className="drawer-access-list">
                          {a2aDrawerGrants.map((g) => {
                            // Backend writes DDB rows keyed by email (matching the
                            // text agent's runtime key). Match that first; fall
                            // back to sub for legacy rows.
                            const userRow = cognitoUsers.find(
                              (u) => u.email === g.userId || u.sub === g.userId,
                            );
                            const label =
                              g.userId === '__global__'
                                ? '__global__'
                                : userRow?.email || g.userId;
                            return (
                              <li key={g.userId}>
                                <strong>{label}</strong> → {g.skillIds.join(', ')}
                              </li>
                            );
                          })}
                        </ul>
                      )}
                      <CloudscapeBox color="text-body-secondary" fontSize="body-s">
                        {t('integrations.a2a.access.editHint')}
                      </CloudscapeBox>
                    </div>
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

      {/* Add User modal — shared across tabs (rendered once in Identity flow) */}
      <Modal
        visible={showAddUserModal}
        onDismiss={() => setShowAddUserModal(false)}
        header={t('overview.addUserModalTitle')}
        footer={
          <CloudscapeBox float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowAddUserModal(false)}>
                {t('overview.cancel')}
              </Button>
              <Button variant="primary" loading={addingUser} onClick={handleCreateUser}>
                {t('overview.addUserSubmit')}
              </Button>
            </SpaceBetween>
          </CloudscapeBox>
        }
      >
        <FormField label={t('overview.addUserEmailLabel')}>
          <Input
            value={newUserEmail}
            placeholder={t('overview.addUserEmailPlaceholder')}
            onChange={({ detail }) => setNewUserEmail(detail.value)}
            onKeyDown={({ detail }) => { if (detail.key === 'Enter') handleCreateUser(); }}
          />
        </FormField>
      </Modal>

      {/* Show generated password after successful user creation */}
      <Modal
        visible={!!createdUser}
        onDismiss={() => setCreatedUser(null)}
        header={t('identity.userCreatedTitle')}
        footer={
          <CloudscapeBox float="right">
            <Button variant="primary" onClick={() => setCreatedUser(null)}>
              {t('overview.cancel')}
            </Button>
          </CloudscapeBox>
        }
      >
        {createdUser && (
          <SpaceBetween size="m">
            <Alert type="warning">
              {t('identity.userCreatedWarn')}
            </Alert>
            <FormField label={t('overview.colEmail')}>
              <Input value={createdUser.email} readOnly />
            </FormField>
            <FormField label={t('identity.passwordLabel')}>
              <SpaceBetween size="xs" direction="horizontal">
                <div style={{ flex: 1, minWidth: 280 }}>
                  <Input value={createdUser.password} readOnly />
                </div>
                <Button iconName="copy"
                        onClick={() => {
                          navigator.clipboard?.writeText(createdUser.password)
                            .then(() => setSuccess(t('identity.passwordCopied')))
                            .catch(() => {});
                        }}>
                  {t('identity.copyPassword')}
                </Button>
              </SpaceBetween>
            </FormField>
          </SpaceBetween>
        )}
      </Modal>
    </div>
  );
};

export default AdminConsole;
