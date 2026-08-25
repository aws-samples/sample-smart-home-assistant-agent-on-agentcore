import React from 'react';
import Box from '@cloudscape-design/components/box';
import Table from '@cloudscape-design/components/table';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Badge from '@cloudscape-design/components/badge';
import Popover from '@cloudscape-design/components/popover';
import Icon from '@cloudscape-design/components/icon';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import { DashboardRelease } from '../../api/adminApi';
import { stamp } from './format';
import { useI18n } from '../../i18n';
import {
  useResizableTables, W_NUM, W_BADGE, W_STATUS, W_DATE, W_NAME, W_WIDE,
} from '../tableColumns';

interface Props {
  release?: DashboardRelease;
  loading: boolean;
}

/**
 * #5 Active version & release state — panel body.
 *
 * A table, not a chart: these are identifiers and states, nothing with a
 * magnitude worth plotting.
 *
 * The rollout stage is DERIVED. AgentCore Runtime endpoints have no
 * Shadow/Canary/Percentage/Full traffic-split field; this project implements
 * gradual rollout with Gateway A/B tests plus per-tenant routing rows, so the
 * stage is inferred from those two and flagged as a project-specific reading.
 * "Shadow" has no implementation here and never appears.
 */
export function ReleaseTable({ release, loading }: Props) {
  const resizable = useResizableTables();
  const { t } = useI18n();
  const endpoints = release?.endpoints ?? [];
  const history = release?.rollbackHistory ?? [];

  return (
    <SpaceBetween size="s">
      <SpaceBetween direction="horizontal" size="l">
        <div>
          <Box variant="awsui-key-label">{t('dashboard.release.latestVersion')}</Box>
          <Box fontSize="heading-l" fontWeight="bold">{release?.latestVersion ?? '--'}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">
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
          </Box>
          <Box padding={{ top: 'xxs' }}>
            <Badge color={release?.abRunning ? 'blue' : 'green'}>
              {release?.rolloutStage ?? '--'}
            </Badge>
          </Box>
        </div>
        <div>
          <Box variant="awsui-key-label">{t('dashboard.release.tenantOverrides')}</Box>
          <Box fontSize="heading-l" fontWeight="bold">{release?.tenantOverrides ?? 0}</Box>
        </div>
      </SpaceBetween>

      <Table
        variant="embedded"
        contentDensity="compact"
        loading={loading && !release}
        loadingText={t('dashboard.loading')}
        items={endpoints}
        trackBy="name"
        {...resizable<typeof endpoints[number]>('dashboard-releases', [
          { id: 'name', width: W_NAME, header: t('dashboard.release.endpoint'), cell: (e) => e.name },
          { id: 'live', width: W_BADGE, header: t('dashboard.release.liveVersion'), cell: (e) => e.liveVersion || '--' },
          {
            id: 'status', width: W_STATUS,
            header: t('dashboard.release.status'),
            cell: (e) => (
              <StatusIndicator type={e.status === 'READY' ? 'success' : 'pending'}>
                {e.status}
              </StatusIndicator>
            ),
          },
          { id: 'updated', header: t('dashboard.release.lastUpdated'), cell: (e) => stamp(e.lastUpdatedAt) },
        ])}
        empty={
          <Box textAlign="center" padding="m" color="text-body-secondary">
            {t('dashboard.noData')}
          </Box>
        }
      />

      {/* Audit trail: useful when investigating, noise the rest of the time. */}
      <ExpandableSection
        headerText={t('dashboard.release.historyTitle')}
        variant="footer"
        headerCounter={history.length ? `(${history.length})` : undefined}
      >
        <SpaceBetween size="xs">
          <Box variant="small" color="text-body-secondary">
            {t('dashboard.release.historyDesc').replace(
              '{days}', String(release?.historyRetentionDays ?? 90),
            )}
          </Box>
          <Table
            variant="embedded"
            contentDensity="compact"
            items={history}
            {...resizable<typeof history[number]>('dashboard-release-history', [
              { id: 'time', width: W_DATE, header: t('dashboard.release.eventTime'), cell: (h) => stamp(h.eventTime) },
              { id: 'endpoint', width: W_WIDE, header: t('dashboard.release.endpoint'), cell: (h) => h.endpointName || '--' },
              { id: 'version', width: W_NUM, header: t('dashboard.release.targetVersion'), cell: (h) => h.targetVersion || '--' },
              { id: 'user', header: t('dashboard.release.actor'), cell: (h) => h.username || '--' },
            ])}
            empty={
              <Box textAlign="center" padding="m" color="text-body-secondary">
                {t('dashboard.release.historyEmpty')}
              </Box>
            }
          />
        </SpaceBetween>
      </ExpandableSection>
    </SpaceBetween>
  );
}
