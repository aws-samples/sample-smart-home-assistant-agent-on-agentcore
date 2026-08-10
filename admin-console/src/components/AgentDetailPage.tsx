import React, { useCallback, useEffect, useMemo, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Container from '@cloudscape-design/components/container';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Table from '@cloudscape-design/components/table';
import {
  CognitoUserInfo,
  FleetAgent,
  PromptRecord,
  deleteAgentPrompt,
  getAgentPrompt,
  saveAgentPrompt,
} from '../api/adminApi';
import { PromptEditorCard } from './AdminConsole';
import { useI18n } from '../i18n';

/**
 * One agent, with its prompt editable.
 *
 * The prompt of the orchestrator and the voice runtime has been governable from
 * the console since the additive-prompt work; a specialist's was whatever
 * `system_prompt.md` said in the image it was built from, so retuning one meant a
 * redeploy. That is backwards — a specialist is exactly the thing an operator
 * wants to adjust without shipping a container.
 *
 * The editor is the same `PromptEditorCard` the Prompt tab uses, so the two
 * screens cannot drift on what "Revert to Default" or the additive per-user scope
 * mean. What differs is the agent it is pointed at: the sub-agent is addressed by
 * its AgentCard name, the one identifier the console, its Registry record and the
 * running container each derive independently.
 */

interface Props {
  agent: FleetAgent;
  onBack: () => void;
  cognitoUsers: CognitoUserInfo[];
}

/** Rows for the editor, scaled to the prompt it holds. */
function editorRowsFor(agent: FleetAgent): number {
  if (agent.agentId === 'smarthome') return 18;
  return 14;
}

export function AgentDetailPage({ agent, onBack, cognitoUsers }: Props) {
  const { t, language } = useI18n();

  const [scope, setScope] = useState('__global__');
  const [record, setRecord] = useState<PromptRecord | null>(null);
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  // The prompt key. `text`/`voice` are the built-in runtimes; everything else is
  // addressed by card name, which is what the sub-agent itself reads.
  const promptKey = useMemo(() => {
    if (agent.kind === 'voice') return 'voice';
    if (agent.kind === 'orchestrator') return 'text';
    return agent.displayName || agent.agentId;
  }, [agent]);

  // A Gateway Lambda target has no system prompt, and neither does the A/B
  // variant runtime — it runs the orchestrator's image, so its prompt is the
  // orchestrator's and editing it here would imply otherwise.
  const governable = agent.kind !== 'tool' && agent.kind !== 'variant';

  const load = useCallback(async (forScope: string) => {
    if (!governable) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError('');
    try {
      const rec = await getAgentPrompt(forScope, promptKey);
      setRecord(rec);
      // At global scope with no override the editor starts from the shipped
      // prompt, so an admin edits what the agent actually runs rather than
      // typing into an empty box. At user scope the draft is only the addendum.
      setDraft(
        forScope === '__global__' && !rec.isOverride ? rec.builtinDefault : rec.body,
      );
    } catch (e: any) {
      setError(e?.message || String(e));
      setRecord(null);
    } finally {
      setLoading(false);
    }
  }, [governable, promptKey]);

  useEffect(() => {
    void load(scope);
  }, [load, scope]);

  const label = (language === 'zh' && agent.displayNameZh) || agent.displayName || agent.agentId;

  const scopeLabel = (s: string) => (s === '__global__' ? t('prompts.globalScope') : s);
  const scopeOptions = [
    { value: '__global__', label: t('prompts.globalScope') },
    ...cognitoUsers
      .map((u) => u.email || u.username)
      .filter((s): s is string => !!s)
      .map((s) => ({ value: s, label: s })),
  ];

  const handleSave = async () => {
    setSaving(true);
    setError('');
    setSuccess('');
    try {
      await saveAgentPrompt(scope, promptKey, draft);
      setSuccess(
        t('agentDetail.promptSaved').replace('{agent}', label).replace('{scope}', scopeLabel(scope)),
      );
      await load(scope);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    setSaving(true);
    setError('');
    setSuccess('');
    try {
      await deleteAgentPrompt(scope, promptKey);
      setSuccess(
        t('agentDetail.promptReverted').replace('{agent}', label).replace('{scope}', scopeLabel(scope)),
      );
      await load(scope);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setSaving(false);
    }
  };

  const metric = (n: number | null | undefined) =>
    n === null || n === undefined || !Number.isFinite(n) ? '--' : Math.round(n).toString();

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            description={agent.description || t('agentDetail.noDescription')}
            actions={
              <Button iconName="arrow-left" onClick={onBack}>
                {t('agentDetail.back')}
              </Button>
            }
          >
            {label} <Badge>{t(`agents.kind.${agent.kind}`)}</Badge>
          </Header>
        }
      >
        <ColumnLayout columns={4} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">{t('agents.col.runtime')}</Box>
            <Box variant="small">{agent.runtimeName || '--'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">{t('agents.col.status')}</Box>
            {agent.live ? (
              <StatusIndicator type="success">
                {agent.registryStatus === 'APPROVED'
                  ? t('agents.status.approved')
                  : t('agents.status.live')}
              </StatusIndicator>
            ) : (
              <StatusIndicator type="warning">{t('agents.status.noRuntime')}</StatusIndicator>
            )}
          </div>
          <div>
            <Box variant="awsui-key-label">{t('agents.col.invocations')}</Box>
            <Box variant="small">{metric(agent.invocations)}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">{t('agents.col.errors')}</Box>
            <Box variant="small">{metric(agent.errors)}</Box>
          </div>
        </ColumnLayout>
      </Container>

      {agent.skills?.length > 0 && (
        <Container
          header={
            <Header variant="h3" description={t('agentDetail.skillsDesc')}>
              {t('agentDetail.skills')} ({agent.skills.length})
            </Header>
          }
        >
          <Table
            variant="embedded"
            contentDensity="compact"
            items={agent.skills}
            trackBy="id"
            columnDefinitions={[
              { id: 'id', header: t('agentDetail.skillId'), cell: (s) => s.id },
              { id: 'name', header: t('agentDetail.skillName'), cell: (s) => s.name || s.id },
              {
                id: 'description',
                header: t('agentDetail.skillDescription'),
                cell: (s) => s.description || '--',
              },
            ]}
          />
        </Container>
      )}

      {!governable ? (
        <Alert type="info" header={t('agentDetail.noPromptTitle')}>
          {agent.kind === 'variant'
            ? t('agentDetail.noPromptVariant')
            : t('agentDetail.noPromptTool')}
        </Alert>
      ) : (
        <>
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError('')}>
              {error}
            </Alert>
          )}
          {success && (
            <Alert type="success" dismissible onDismiss={() => setSuccess('')}>
              {success}
            </Alert>
          )}

          <Container
            header={
              <Header variant="h3" description={t('agentDetail.promptDesc')}>
                {t('agentDetail.prompt')}
              </Header>
            }
          >
            <FormField label={t('prompts.userScope')} description={t('agentDetail.scopeHint')}>
              <div style={{ maxWidth: 360 }}>
                <Select
                  selectedOption={
                    scopeOptions.find((o) => o.value === scope) ?? scopeOptions[0]
                  }
                  onChange={({ detail }) => setScope(detail.selectedOption.value as string)}
                  options={scopeOptions}
                />
              </div>
            </FormField>
          </Container>

          {loading || !record ? (
            <Box textAlign="center" padding="l">
              <StatusIndicator type="loading">{t('prompts.loading')}</StatusIndicator>
            </Box>
          ) : (
            <div style={{ display: 'flex' }}>
              <PromptEditorCard
                agentType={promptKey}
                title={label}
                hint={t('agentDetail.promptCardHint')}
                rows={editorRowsFor(agent)}
                scope={scope}
                record={record}
                draft={draft}
                onChangeDraft={setDraft}
                onSave={handleSave}
                onDiscard={() =>
                  setDraft(
                    scope === '__global__' && !record.isOverride
                      ? record.builtinDefault
                      : record.body,
                  )
                }
                onReset={handleReset}
                saving={saving}
              />
            </div>
          )}
        </>
      )}
    </SpaceBetween>
  );
}

export default AgentDetailPage;
