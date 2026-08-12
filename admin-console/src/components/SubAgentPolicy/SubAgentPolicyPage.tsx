/**
 * Build → SubAgent Policy: which A2A specialists each user may reach.
 *
 * This was a section inside the Tool Policy permissions panel until 2026-08-12. It
 * moved out for two reasons. It had no `__global__` entry point, because that panel
 * is keyed on a selected Cognito user and a global default has no user to select.
 * And an A2A grant is a different kind of object from an MCP tool grant: the unit is
 * an AgentCard skill on a separately deployed agent, not a tool on this gateway.
 *
 * A grant is a **Cognito group** (`a2a-<agent>.<skill>`), checked by each sub-agent
 * Runtime's own JWT authorizer before any of our code runs. So this page writes
 * intent to DynamoDB and the backend materialises it into group membership; the two
 * stores are shown side by side because a save can succeed at the first and fail at
 * the second, and only the second is what actually enforces.
 *
 * Three things here exist because of how this fails rather than how it works:
 *   - the effective-permission preview, because per-user grants REPLACE global per
 *     sub-agent, so ticking one skill can mean taking three away;
 *   - the confirmation before narrowing, because saving signs affected users out;
 *   - the sync status and reconcile action, because a one-way materialisation drifts.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import CloudscapeBox from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import CloudscapeHeader from '@cloudscape-design/components/header';
import FormField from '@cloudscape-design/components/form-field';
import Modal from '@cloudscape-design/components/modal';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import StatusIndicator from '@cloudscape-design/components/status-indicator';

import {
  A2AAvailableAgent,
  A2AGroupSync,
  A2AReconcileResult,
  CognitoUserInfo,
  getUserA2APermissions,
  listCognitoUsers,
  reconcileA2AGrants,
  repairA2AGrants,
  updateUserA2APermissions,
} from '../../api/adminApi';
import { useI18n } from '../../i18n';

const GLOBAL = '__global__';

interface Props {
  error: string;
  success: string;
  clearMessages: () => void;
  setError: (msg: string) => void;
  setSuccess: (msg: string) => void;
}

type Grants = Record<string, string[]>;

/** Per-user grants REPLACE global grants for a sub-agent they both mention.
 *
 *  Mirrors the agent and the backend. A union would be easier to explain but would
 *  remove the ability to narrow one user below the global baseline, and would
 *  silently widen access for anyone already relying on an override. */
function effectiveGrants(globalGrants: Grants, userGrants: Grants): Grants {
  return { ...globalGrants, ...userGrants };
}

/** Whether a failure is API Gateway giving up rather than the work failing.
 *
 *  Group materialisation across every user can outrun the 29s request budget while
 *  continuing to completion in the Lambda. Telling the admin it failed would be
 *  false, and would invite a retry of work already in flight. */
function _looksLikeTimeout(err: any): boolean {
  const msg = String(err?.message || err || '');
  return msg.includes('504') || /timed? ?out/i.test(msg);
}

const SubAgentPolicyPage: React.FC<Props> = ({
  error, success, clearMessages, setError, setSuccess,
}) => {
  const { t } = useI18n();
  const [users, setUsers] = useState<CognitoUserInfo[]>([]);
  const [scope, setScope] = useState<string>(GLOBAL);
  const [agents, setAgents] = useState<A2AAvailableAgent[]>([]);
  const [catalogError, setCatalogError] = useState('');
  const [globalGrants, setGlobalGrants] = useState<Grants>({});
  const [draft, setDraft] = useState<Grants>({});
  const [saved, setSaved] = useState<Grants>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmNarrow, setConfirmNarrow] = useState(false);
  const [sync, setSync] = useState<A2AGroupSync | null>(null);
  const [reconcile, setReconcile] = useState<A2AReconcileResult | null>(null);
  const [reconciling, setReconciling] = useState(false);

  const loadUsers = useCallback(async () => {
    try {
      setUsers(await listCognitoUsers());
    } catch (err: any) {
      setError(err.message);
    }
  }, [setError]);

  /** Load the selected scope's grants, plus global whenever the scope is a user.
   *
   *  Global is needed even when editing one user, because the preview has to show
   *  what is inherited and what is being replaced. */
  const loadScope = useCallback(async (target: string) => {
    setLoading(true);
    try {
      const own = await getUserA2APermissions(target);
      setAgents(own.availableAgents || []);
      setCatalogError(own.catalogError || '');
      setDraft(own.a2aGrants || {});
      setSaved(own.a2aGrants || {});
      if (target === GLOBAL) {
        setGlobalGrants(own.a2aGrants || {});
      } else {
        const g = await getUserA2APermissions(GLOBAL);
        setGlobalGrants(g.a2aGrants || {});
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [setError]);

  useEffect(() => { loadUsers(); }, [loadUsers]);
  useEffect(() => { loadScope(scope); }, [scope, loadScope]);

  const scopeOptions = useMemo(() => [
    { value: GLOBAL, label: t('subagent.globalScope') },
    ...users.map((u) => ({
      value: u.email || u.username || u.sub,
      label: u.email || u.username || u.sub,
    })),
  ], [users, t]);

  const toggle = (recordId: string, skillId: string) => {
    setDraft((prev) => {
      const current = prev[recordId] || [];
      const next = current.includes(skillId)
        ? current.filter((s) => s !== skillId)
        : [...current, skillId].sort();
      // Keep the key with an empty array rather than deleting it: for a per-user
      // scope, "no skills on this sub-agent" is a real instruction that narrows
      // below global, and dropping the key would mean "inherit global" instead.
      return { ...prev, [recordId]: next };
    });
  };

  const dirty = useMemo(
    () => JSON.stringify(
      Object.fromEntries(Object.entries(draft).filter(([, v]) => v.length)))
      !== JSON.stringify(
        Object.fromEntries(Object.entries(saved).filter(([, v]) => v.length))),
    [draft, saved],
  );

  /** Whether saving would take access away from anyone.
   *
   *  Drives the confirmation, because the backend signs affected users out — group
   *  membership is in their token, so a revoke that did not force a refresh would
   *  do nothing for up to an hour. */
  const narrowing = useMemo(() => {
    for (const [recordId, before] of Object.entries(saved)) {
      const after = draft[recordId] || [];
      if (before.some((s) => !after.includes(s))) return true;
    }
    return false;
  }, [draft, saved]);

  const doSave = async () => {
    clearMessages();
    setConfirmNarrow(false);
    setSaving(true);
    try {
      const cleaned = Object.fromEntries(
        Object.entries(draft).filter(([, skills]) => skills.length));
      const result = await updateUserA2APermissions(scope, cleaned);
      setSaved(draft);
      if (scope === GLOBAL) setGlobalGrants(draft);
      setSync(result || null);
      if (result && !result.ok) {
        // Intent saved, enforcement did not follow. Saying only "saved" here would
        // be untrue in the way that matters.
        setError(t('subagent.syncFailed')
          .replace('{error}', result.error || (result.errors || []).join('; ')));
      } else {
        const out = (result?.signedOut || []).length;
        setSuccess(out
          ? t('subagent.savedAndSignedOut').replace('{n}', String(out))
          : t('subagent.saved'));
      }
    } catch (err: any) {
      // A gateway timeout here does NOT mean the save failed. The intent write
      // happens first and the group materialisation continues server-side after API
      // Gateway gives up at 29s — measured applying 8 sub-agents to 40 users, which
      // is ~680 Cognito calls. Reporting "failed" would be wrong and would invite a
      // retry that does the same work again.
      setError(_looksLikeTimeout(err)
        ? t('subagent.savedStillApplying')
        : err.message);
    } finally {
      setSaving(false);
    }
  };

  const runReconcile = async (repair: boolean) => {
    clearMessages();
    setReconciling(true);
    try {
      if (repair) {
        const result = await repairA2AGrants(GLOBAL);
        setSuccess(t('subagent.repaired')
          .replace('{n}', String((result.users || []).length)));
        setReconcile(await reconcileA2AGrants(GLOBAL));
      } else {
        const result = await reconcileA2AGrants(GLOBAL);
        setReconcile(result);
        const drifted = (result.outOfSync || []).length;
        setSuccess(drifted
          ? t('subagent.reconcileDrift').replace('{n}', String(drifted))
          : t('subagent.reconcileClean'));
      }
    } catch (err: any) {
      setError(_looksLikeTimeout(err)
        ? t('subagent.repairStillApplying')
        : err.message);
    } finally {
      setReconciling(false);
    }
  };

  const effective = effectiveGrants(globalGrants, draft);
  const agentName = (recordId: string) =>
    agents.find((a) => a.recordId === recordId)?.name || recordId;

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
      {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}

      <Alert type="info" header={t('subagent.howItWorksTitle')}>
        {t('subagent.howItWorks')}
      </Alert>

      {/* A failed Registry read and an empty registry produce the same empty list,
          and only one of them is worth an admin's time. */}
      {catalogError && (
        <Alert type="warning" header={t('subagent.catalogFailedTitle')}>
          {t('subagent.catalogFailed').replace('{error}', catalogError)}
        </Alert>
      )}

      <Container
        header={
          <CloudscapeHeader
            variant="h2"
            description={t('subagent.scopeHint')}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  iconName="refresh"
                  loading={reconciling}
                  onClick={() => runReconcile(false)}
                >
                  {t('subagent.reconcile')}
                </Button>
                <Button
                  variant="primary"
                  loading={saving}
                  disabled={!dirty}
                  onClick={() => (narrowing ? setConfirmNarrow(true) : doSave())}
                >
                  {t('subagent.save')}
                </Button>
              </SpaceBetween>
            }
          >
            {t('subagent.title')}
          </CloudscapeHeader>
        }
      >
        <SpaceBetween size="m">
          <FormField label={t('subagent.scope')}>
            <div style={{ maxWidth: 420 }}>
              <Select
                selectedOption={
                  scopeOptions.find((o) => o.value === scope) ?? scopeOptions[0]
                }
                onChange={({ detail }) => setScope(detail.selectedOption.value as string)}
                options={scopeOptions}
              />
            </div>
          </FormField>

          {loading ? (
            <CloudscapeBox textAlign="center" padding="l"><Spinner /></CloudscapeBox>
          ) : agents.length === 0 ? (
            <CloudscapeBox color="text-body-secondary" padding="s">
              {catalogError ? t('subagent.noneBecauseError') : t('subagent.none')}
            </CloudscapeBox>
          ) : (
            <div className="perm-a2a-list">
              {agents.map((agent) => {
                const granted = draft[agent.recordId] || [];
                const open = !!expanded[agent.recordId];
                return (
                  <div key={agent.recordId} className="perm-a2a-agent">
                    <div
                      className="perm-a2a-agent-header"
                      onClick={() => setExpanded((p) => ({
                        ...p, [agent.recordId]: !p[agent.recordId],
                      }))}
                    >
                      <span className="perm-a2a-chevron">{open ? '▾' : '▸'}</span>
                      <span className="perm-a2a-name">{agent.name}</span>
                      <span className="perm-a2a-count">
                        {t('subagent.grantedCount')
                          .replace('{n}', String(granted.length))
                          .replace('{total}', String(agent.skills.length))}
                      </span>
                      {granted.length > 0 && (
                        <Badge color="green">{t('subagent.granted')}</Badge>
                      )}
                    </div>
                    {open && (
                      <div className="perm-a2a-skills">
                        {agent.skills.map((skill) => (
                          <label key={skill.id} className="perm-a2a-skill-item">
                            <input
                              type="checkbox"
                              checked={granted.includes(skill.id)}
                              onChange={() => toggle(agent.recordId, skill.id)}
                            />
                            <span className="perm-a2a-skill-id">{skill.id}</span>
                            <span className="perm-a2a-skill-desc">
                              {skill.description}
                            </span>
                          </label>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </SpaceBetween>
      </Container>

      {/* The preview exists because the checkbox tree cannot show replacement. A
          user with one skill ticked has been cut from four to one, not given one. */}
      {scope !== GLOBAL && !loading && (
        <Container
          header={
            <CloudscapeHeader variant="h3" description={t('subagent.previewHint')}>
              {t('subagent.previewTitle')}
            </CloudscapeHeader>
          }
        >
          {Object.keys(effective).filter((k) => (effective[k] || []).length).length === 0 ? (
            <CloudscapeBox color="text-body-secondary">
              {t('subagent.previewEmpty')}
            </CloudscapeBox>
          ) : (
            <SpaceBetween size="xs">
              {Object.entries(effective)
                .filter(([, skills]) => skills.length)
                .map(([recordId, skills]) => {
                  const overridden = (draft[recordId] || []).length > 0;
                  return (
                    <div key={recordId}>
                      <code>{agentName(recordId)}</code>{' → '}
                      <code>[{skills.join(', ')}]</code>{'  '}
                      {overridden ? (
                        <Badge color="blue">{t('subagent.replacesGlobal')}</Badge>
                      ) : (
                        <Badge>{t('subagent.inheritedFromGlobal')}</Badge>
                      )}
                    </div>
                  );
                })}
            </SpaceBetween>
          )}
        </Container>
      )}

      {sync && (
        <Container header={<CloudscapeHeader variant="h3">{t('subagent.syncTitle')}</CloudscapeHeader>}>
          <SpaceBetween size="xs">
            <div>
              {sync.ok
                ? <StatusIndicator type="success">{t('subagent.syncOk')}</StatusIndicator>
                : <StatusIndicator type="error">{t('subagent.syncBad')}</StatusIndicator>}
            </div>
            {(sync.users || []).filter((u) => u.added.length || u.removed.length)
              .map((u) => (
                <div key={u.username}>
                  <code>{u.username}</code>
                  {u.added.length > 0 && <> +{u.added.join(', ')}</>}
                  {u.removed.length > 0 && <> −{u.removed.join(', ')}</>}
                </div>
              ))}
            {(sync.signedOut || []).length > 0 && (
              <CloudscapeBox color="text-status-info">
                {t('subagent.signedOutList').replace('{users}', (sync.signedOut || []).join(', '))}
              </CloudscapeBox>
            )}
          </SpaceBetween>
        </Container>
      )}

      {reconcile && (
        <Container
          header={
            <CloudscapeHeader
              variant="h3"
              description={t('subagent.reconcileHint')}
              actions={
                (reconcile.outOfSync || []).length > 0 && (
                  <Button loading={reconciling} onClick={() => runReconcile(true)}>
                    {t('subagent.repair')}
                  </Button>
                )
              }
            >
              {t('subagent.reconcileTitle')}
            </CloudscapeHeader>
          }
        >
          {(reconcile.outOfSync || []).length === 0 ? (
            <StatusIndicator type="success">{t('subagent.reconcileClean')}</StatusIndicator>
          ) : (
            <SpaceBetween size="xs">
              {reconcile.users.filter((u) => !u.inSync).map((u) => (
                <div key={u.username}>
                  <code>{u.username}</code>
                  {u.error
                    ? <> — {u.error}</>
                    : <>
                        {(u.missing || []).length > 0 && <> {t('subagent.missing')}: {(u.missing || []).join(', ')}</>}
                        {(u.extra || []).length > 0 && <> {t('subagent.extra')}: {(u.extra || []).join(', ')}</>}
                      </>}
                </div>
              ))}
            </SpaceBetween>
          )}
        </Container>
      )}

      <Modal
        visible={confirmNarrow}
        onDismiss={() => setConfirmNarrow(false)}
        header={t('subagent.confirmTitle')}
        footer={
          <CloudscapeBox float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setConfirmNarrow(false)}>{t('subagent.cancel')}</Button>
              <Button variant="primary" loading={saving} onClick={doSave}>
                {t('subagent.confirmSave')}
              </Button>
            </SpaceBetween>
          </CloudscapeBox>
        }
      >
        <SpaceBetween size="s">
          <div>
            {scope === GLOBAL
              ? t('subagent.confirmGlobal')
              : t('subagent.confirmUser').replace('{user}', scope)}
          </div>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export default SubAgentPolicyPage;
