import React from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Box from '@cloudscape-design/components/box';
import Table from '@cloudscape-design/components/table';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Badge from '@cloudscape-design/components/badge';
import Popover from '@cloudscape-design/components/popover';
import Icon from '@cloudscape-design/components/icon';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import { DashboardRelease } from '../../api/adminApi';
import { useI18n } from '../../i18n';

interface Props {
  release?: DashboardRelease;
  loading: boolean;
}

/**
 * #5 Active version & release state.
 *
 * A table, not a chart — these are identifiers and states, and nothing here has
 * a magnitude worth plotting.
 *
 * The rollout stage is DERIVED, not read from AgentCore: Runtime Endpoints have
 * no Shadow/Canary/Percentage/Full traffic-split field. This project implements
 * gradual rollout with Gateway A/B tests plus per-tenant routing rows, so the
 * stage is inferred from those two and labelled as a project-specific reading.
 * "Shadow" has no implementation here and is never shown.
 */
export function ReleaseTable({ release, loading }: Props) {
  const { t } = useI18n();
  const endpoints = release?.endpoints ?? [];
  const history = release?.rollbackHistory ?? [];

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header variant="h3" description={t('dashboard.release.desc')}>
            {t('dashboard.release.title')}
          </Header>
        }
      >
        <SpaceBetween size="m">
          <KeyValuePairs
            columns={3}
            items={[
              {
                label: t('dashboard.release.latestVersion'),
                value: release?.latestVersion ?? '--',
              },
              {
                label: (
                  <>
                    {t('dashboard.release.rolloutStage')}{' '}
                    <Popover
                      dismissButton={false}
                      position="top"
                      size="medium"
                      triggerType="custom"
                      header={t('dashboard.release.derivedTitle')}
                      content={<Box variant="p">{t('dashboard.release.derivedBody')}</Box>}
                    >
                      <span style={{ cursor: 'help' }}>
                        <Icon name="status-info" size="small" variant="subtle" />
                      </span>
                    </Popover>
                  </>
                ) as any,
                value: (
                  <SpaceBetween direction="horizontal" size="xs">
                    <Badge color={release?.abRunning ? 'blue' : 'green'}>
                      {release?.rolloutStage ?? '--'}
                    </Badge>
                    <Box variant="small" color="text-body-secondary">
                      {t('dashboard.release.derivedHint')}
                    </Box>
                  </SpaceBetween>
                ),
              },
              {
                label: t('dashboard.release.tenantOverrides'),
                value: String(release?.tenantOverrides ?? 0),
              },
            ]}
          />

          <Table
            variant="embedded"
            loading={loading && !release}
            loadingText={t('dashboard.loading')}
            items={endpoints}
            trackBy="name"
            columnDefinitions={[
              { id: 'name', header: t('dashboard.release.endpoint'), cell: (e) => e.name },
              { id: 'live', header: t('dashboard.release.liveVersion'), cell: (e) => e.liveVersion || '--' },
              {
                id: 'status',
                header: t('dashboard.release.status'),
                cell: (e) => (
                  <StatusIndicator type={e.status === 'READY' ? 'success' : 'pending'}>
                    {e.status}
                  </StatusIndicator>
                ),
              },
              {
                id: 'updated',
                header: t('dashboard.release.lastUpdated'),
                cell: (e) => (e.lastUpdatedAt ? e.lastUpdatedAt.slice(0, 19) : '--'),
              },
            ]}
            empty={
              <Box textAlign="center" padding="m" color="text-body-secondary">
                {t('dashboard.noData')}
              </Box>
            }
          />
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header
            variant="h3"
            description={t('dashboard.release.historyDesc').replace(
              '{days}',
              String(release?.historyRetentionDays ?? 90),
            )}
          >
            {t('dashboard.release.historyTitle')}
          </Header>
        }
      >
        <Table
          variant="embedded"
          items={history}
          columnDefinitions={[
            { id: 'time', header: t('dashboard.release.eventTime'), cell: (h) => h.eventTime.slice(0, 19) },
            { id: 'endpoint', header: t('dashboard.release.endpoint'), cell: (h) => h.endpointName || '--' },
            { id: 'version', header: t('dashboard.release.targetVersion'), cell: (h) => h.targetVersion || '--' },
            { id: 'user', header: t('dashboard.release.actor'), cell: (h) => h.username || '--' },
          ]}
          empty={
            <Box textAlign="center" padding="m" color="text-body-secondary">
              {t('dashboard.release.historyEmpty')}
            </Box>
          }
        />
      </Container>
    </SpaceBetween>
  );
}
