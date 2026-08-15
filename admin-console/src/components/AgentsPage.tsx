import React, { useCallback, useEffect, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Link from '@cloudscape-design/components/link';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Table from '@cloudscape-design/components/table';
import { CognitoUserInfo, FleetAgent, listAgentFleet, listCognitoUsers } from '../api/adminApi';
import { AgentDetailPage } from './AgentDetailPage';
import { useI18n } from '../i18n';

/**
 * The Agents page — the fleet as one list.
 *
 * Until now "agent" was not an entity in this console: the first-class entities
 * were the Cognito user and the skill, and an agent appeared only as a two-value
 * `text | voice` enum inside the prompt and optimization screens. With an
 * orchestrator, five specialists and a navigation tool, nothing answered "what
 * agents exist, where do they run, and is any of them failing".
 *
 * The list is derived server-side from the runtime ARNs, the approved Registry
 * records and optional metadata rows — never from a hardcoded list here. That is
 * what makes a newly deployed sub-agent appear with no frontend change, the same
 * property the A2A authorisation tree already has.
 */

/** Counts read as counts: "3 errors", not "3.0". */
function count(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '--';
  return Math.abs(n) < 1000 ? Math.round(n).toString() : `${(n / 1000).toFixed(1)}k`;
}

function latency(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '--';
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`;
}

const KIND_COLOR: Record<string, 'blue' | 'green' | 'grey' | 'severity-neutral'> = {
  orchestrator: 'blue',
  specialist: 'green',
  voice: 'grey',
  // The bundles runtime: same image as the orchestrator, only reached when a
  // tenant is in ab-bundles mode. Listed so its token spend is attributable.
  variant: 'grey',
  tool: 'severity-neutral',
};

export function AgentsPage() {
  const { t, language } = useI18n();
  const [agents, setAgents] = useState<FleetAgent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  // The list and the detail view are one tab rather than two routable pages: the
  // console has no router, and a second hash route would need its agent id to
  // survive a reload, which means re-fetching the fleet to resolve it anyway.
  const [selected, setSelected] = useState<FleetAgent | null>(null);
  // Scope options for the prompt editor. Fetched here, once, rather than by the
  // detail view — otherwise every row click re-lists Cognito.
  const [cognitoUsers, setCognitoUsers] = useState<CognitoUserInfo[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setAgents(await listAgentFleet());
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    // A failure here only costs the per-user scope options, so the page still
    // renders and the global prompt is still editable.
    listCognitoUsers()
      .then(setCognitoUsers)
      .catch(() => setCognitoUsers([]));
  }, []);

  const label = (a: FleetAgent) =>
    (language === 'zh' && a.displayNameZh) || a.displayName || a.agentId;

  // An approved Registry record with no live runtime behind it. Called out rather
  // than hidden: it means either the runtime was torn down and the record left
  // orphaned, or a deploy skipped its `patch-text-agent` step so the dashboard's
  // runtime allowlist never learned the ARN. The second is invisible otherwise.
  const orphaned = agents.filter((a) => !a.live);

  if (selected) {
    // Re-read the row from the freshly loaded fleet so metrics are not frozen at
    // whatever they were when the row was clicked.
    const current = agents.find((a) => a.agentId === selected.agentId) ?? selected;
    return (
      <AgentDetailPage
        agent={current}
        onBack={() => setSelected(null)}
        cognitoUsers={cognitoUsers}
      />
    );
  }

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            description={t('agents.desc')}
            counter={agents.length ? `(${agents.length})` : undefined}
            actions={
              <Button iconName="refresh" onClick={() => void load()} loading={loading}>
                {t('common.refresh')}
              </Button>
            }
          >
            {t('agents.title')}
          </Header>
        }
      >
        <SpaceBetween size="m">
          {error && (
            <Alert type="error" header={t('agents.loadFailed')}>
              {error}
            </Alert>
          )}
          {orphaned.length > 0 && (
            <Alert type="warning" header={t('agents.orphanTitle')}>
              {t('agents.orphanDesc')}{' '}
              {orphaned.map((a) => a.displayName || a.agentId).join(', ')}
            </Alert>
          )}
          <Table
            variant="embedded"
            contentDensity="compact"
            loading={loading && agents.length === 0}
            loadingText={t('agents.loading')}
            items={agents}
            trackBy="agentId"
            empty={
              <Box textAlign="center" color="inherit" padding={{ vertical: 'm' }}>
                <b>{t('agents.emptyTitle')}</b>
                <Box variant="p" color="text-body-secondary">
                  {t('agents.emptyDesc')}
                </Box>
              </Box>
            }
            columnDefinitions={[
              {
                id: 'name',
                header: t('agents.col.name'),
                cell: (a) => (
                  <Link
                    href="#"
                    onFollow={(e) => {
                      e.preventDefault();
                      setSelected(a);
                    }}
                  >
                    {label(a)}
                  </Link>
                ),
                sortingField: 'displayName',
              },
              {
                id: 'kind',
                header: t('agents.col.kind'),
                cell: (a) => (
                  <Badge color={KIND_COLOR[a.kind] || 'grey'}>
                    {t(`agents.kind.${a.kind}`)}
                  </Badge>
                ),
              },
              {
                id: 'status',
                header: t('agents.col.status'),
                cell: (a) =>
                  a.live ? (
                    <StatusIndicator type="success">
                      {a.registryStatus === 'APPROVED'
                        ? t('agents.status.approved')
                        : t('agents.status.live')}
                    </StatusIndicator>
                  ) : (
                    <StatusIndicator type="warning">
                      {t('agents.status.noRuntime')}
                    </StatusIndicator>
                  ),
              },
              {
                id: 'skills',
                header: t('agents.col.skills'),
                // The count, with the ids as the tooltip-ish secondary line: six
                // agents' worth of skill ids inline makes the table unreadable.
                cell: (a) =>
                  a.skills?.length ? (
                    <span title={a.skills.map((s) => s.id).join(', ')}>
                      {a.skills.length}
                    </span>
                  ) : (
                    '--'
                  ),
              },
              {
                id: 'runtime',
                // The runtime ID, not the runtime name. The name
                // (`sha2aenergy_sha2aenergy`) is only the id with its suffix cut off,
                // and the suffix is what identifies the deployment — it is what every
                // CloudWatch log group, span and `aws bedrock-agentcore-control` call
                // is keyed on. The agent's identity is the Agent column's job, and
                // that comes from the Registry record.
                header: t('agents.col.runtime'),
                cell: (a) => (
                  <Box variant="small" color="text-body-secondary">
                    {a.runtimeId || '--'}
                  </Box>
                ),
              },
              {
                id: 'invocations',
                header: t('agents.col.invocations'),
                cell: (a) => count(a.invocations),
              },
              {
                id: 'errors',
                header: t('agents.col.errors'),
                cell: (a) =>
                  a.errors ? (
                    <StatusIndicator type="error">{count(a.errors)}</StatusIndicator>
                  ) : (
                    count(a.errors)
                  ),
              },
              {
                id: 'latency',
                header: t('agents.col.latency'),
                cell: (a) => latency(a.latencyP95Ms),
              },
            ]}
          />
          <Box variant="small" color="text-body-secondary">
            {t('agents.metricsNote')}
          </Box>
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}

export default AgentsPage;
