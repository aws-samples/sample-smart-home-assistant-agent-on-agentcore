import React from 'react';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator, { StatusIndicatorProps } from '@cloudscape-design/components/status-indicator';
import Table from '@cloudscape-design/components/table';
import { useI18n } from '../../i18n';
import type { RegistryEvent, RegistryEventsResult } from '../../api/adminApi';
import {
  useResizableTables, W_BADGE, W_DATE, W_EMAIL, W_NAME, W_STATUS,
} from '../tableColumns';

interface Props {
  data: RegistryEventsResult | null;
  loading: boolean;
  /** Non-empty when the last fetch failed. Rendered as a lookup failure, never as
   *  an empty feed — the two must not look alike. */
  error: string;
  onRefresh: () => void;
  /** Take the admin to where this record is decided. Only called for rows whose
   *  recordType is known; the button is disabled otherwise. */
  onReview: (evt: RegistryEvent) => void;
}

const TRANSITION_STATUS: Record<RegistryEvent['transition'], StatusIndicatorProps.Type> = {
  DRAFT: 'info',
  PENDING_APPROVAL: 'pending',
  APPROVED: 'success',
  REJECTED: 'error',
  DEPRECATED: 'stopped',
};

/**
 * Registry activity — what AWS Agent Registry told EventBridge, newest first.
 *
 * Pure presentation: the data, the 30s polling and the refresh live in
 * AdminConsole, because the pending count is also shown on the Overview sub-tab
 * label while this panel is not mounted, and one fetch should feed both.
 *
 * "Highlight" here is a badge plus bold text plus the info alert above the table.
 * Cloudscape's Table does not colour rows, and the Skills review queue already
 * teaches the reader that a badge in the name column is the thing to look at.
 */
export function RegistryActivityPanel({ data, loading, error, onRefresh, onReview }: Props) {
  const { t } = useI18n();
  const resizable = useResizableTables();
  const events = data?.events ?? [];
  const pending = data?.pendingCount ?? 0;

  const typeLabel = (evt: RegistryEvent) => {
    if (evt.recordType === 'SKILL') return t('registryEvents.type.skill');
    if (evt.recordType === 'AGENT') return t('registryEvents.type.agent');
    return evt.recordType || t('registryEvents.type.unknown');
  };

  return (
    <SpaceBetween size="m">
      {error && (
        <Alert type="warning" header={t('registryEvents.loadFailedTitle')}>
          {t('registryEvents.loadFailed').replace('{error}', error)}
        </Alert>
      )}
      {!error && pending > 0 && (
        <Alert type="info">
          {t('registryEvents.pendingAlert').replace('{count}', String(pending))}
        </Alert>
      )}
      <Table
        header={
          <Header
            variant="h2"
            counter={data ? `(${events.length})` : undefined}
            description={t('registryEvents.desc')}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                {data?.generatedAt && (
                  <Box variant="small" color="text-body-secondary" padding={{ top: 'xxs' }}>
                    {t('registryEvents.updatedAt')
                      .replace('{time}', new Date(data.generatedAt).toLocaleTimeString())}
                  </Box>
                )}
                <Button iconName="refresh" loading={loading} onClick={onRefresh}>
                  {t('registryEvents.refresh')}
                </Button>
              </SpaceBetween>
            }
          >
            {t('registryEvents.title')}
          </Header>
        }
        loading={loading && !data}
        loadingText={t('registryEvents.loading')}
        items={events}
        trackBy="eventKey"
        {...resizable<RegistryEvent>('registry-activity', [
          {
            id: 'type', width: W_BADGE,
            header: t('registryEvents.col.type'),
            cell: (evt) => (
              <Badge color={evt.recordType ? 'blue' : 'grey'}>{typeLabel(evt)}</Badge>
            ),
          },
          {
            id: 'record', width: W_NAME + 80,
            header: t('registryEvents.col.record'),
            cell: (evt) => {
              const label = evt.name
                ? evt.name
                : <span title={evt.enrichError || ''}>{evt.recordId} · {t('registryEvents.detailsUnavailable')}</span>;
              return (
                <SpaceBetween direction="horizontal" size="xs">
                  {evt.actionable ? <b>{label}</b> : <span>{label}</span>}
                  {evt.recordVersion && (
                    <Box variant="small" color="text-body-secondary">v{evt.recordVersion}</Box>
                  )}
                  {evt.actionable && <Badge color="red">{t('registryEvents.needsReview')}</Badge>}
                </SpaceBetween>
              );
            },
          },
          {
            id: 'transition', width: W_STATUS,
            header: t('registryEvents.col.transition'),
            cell: (evt) => (
              <span title={evt.statusReason || ''}>
                <StatusIndicator type={TRANSITION_STATUS[evt.transition] || 'info'}>
                  {t(`registryEvents.transition.${evt.transition}`)}
                </StatusIndicator>
              </span>
            ),
          },
          {
            id: 'publishedBy', width: W_EMAIL,
            header: t('registryEvents.col.publishedBy'),
            cell: (evt) => evt.publishedBy || '—',
          },
          {
            id: 'when', width: W_DATE,
            header: t('registryEvents.col.when'),
            cell: (evt) => (evt.occurredAt ? new Date(evt.occurredAt).toLocaleString() : '—'),
          },
          {
            id: 'actions',
            header: t('registryEvents.col.actions'),
            cell: (evt) => {
              if (!evt.actionable) return '—';
              const known = evt.recordType === 'SKILL' || evt.recordType === 'AGENT';
              return (
                <span title={known ? '' : t('registryEvents.reviewUnavailable')}>
                  <Button variant="primary" disabled={!known} onClick={() => onReview(evt)}>
                    {t('registryEvents.review')}
                  </Button>
                </span>
              );
            },
          },
        ])}
        empty={
          <Box textAlign="center" padding="m">
            <b>{t('registryEvents.empty')}</b>
            <Box variant="p" color="text-body-secondary">
              {error ? t('integrations.skills.emptyBecauseError') : t('registryEvents.emptyHint')}
            </Box>
          </Box>
        }
      />
    </SpaceBetween>
  );
}
