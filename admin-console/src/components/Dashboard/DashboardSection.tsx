import React, { useCallback, useEffect, useState } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Select from '@cloudscape-design/components/select';
import Button from '@cloudscape-design/components/button';
import Alert from '@cloudscape-design/components/alert';
import FormField from '@cloudscape-design/components/form-field';
import {
  DashboardDim,
  DashboardFastResponse,
  DashboardRange,
  DashboardSpansResponse,
  getDashboardFast,
  getDashboardSpans,
} from '../../api/adminApi';
import { ChartTheme } from './palette';
import { KpiRow } from './KpiRow';
import { TokenCharts } from './TokenCharts';
import { BudgetMeter } from './BudgetMeter';
import { EvalPanel } from './EvalPanel';
import { ReleaseTable } from './ReleaseTable';
import { SatisfactionCards } from './SatisfactionCards';
import { useI18n } from '../../i18n';

interface Props {
  theme: ChartTheme;
}

const RANGES: DashboardRange[] = ['24h', '7d', '30d'];
const DIMS: DashboardDim[] = ['user', 'tenant', 'agent'];

/**
 * Agent ops dashboard on the Overview page.
 *
 * Loads in two stages because the token/TTFT figures come from a Logs Insights
 * query that takes ~5-20s, while everything else is a fast metric read. The
 * fast cards paint first and the token cards fill in, so the landing page is
 * never blocked on the slow query.
 *
 * On refetch the previous render is HELD at reduced opacity rather than
 * replaced by skeletons — no layout jump, no flash.
 */
export function DashboardSection({ theme }: Props) {
  const { t } = useI18n();
  const [range, setRange] = useState<DashboardRange>('7d');
  const [dim, setDim] = useState<DashboardDim>('user');

  const [fast, setFast] = useState<DashboardFastResponse | undefined>();
  const [spans, setSpans] = useState<DashboardSpansResponse | undefined>();
  const [fastLoading, setFastLoading] = useState(false);
  const [spansLoading, setSpansLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(
    async (r: DashboardRange, d: DashboardDim, refresh: boolean) => {
      setError('');
      setFastLoading(true);
      setSpansLoading(true);
      // Fire both stages together; the fast one resolves first and paints.
      const fastPromise = getDashboardFast(r, refresh)
        .then((res) => setFast(res))
        .catch((e) => setError(e.message))
        .finally(() => setFastLoading(false));
      const spansPromise = getDashboardSpans(r, d, refresh)
        .then((res) => setSpans(res))
        // A spans failure must not blank the fast cards.
        .catch(() => setSpans(undefined))
        .finally(() => setSpansLoading(false));
      await Promise.allSettled([fastPromise, spansPromise]);
    },
    [],
  );

  useEffect(() => {
    void load(range, dim, false);
  }, [range, dim, load]);

  const loading = fastLoading || spansLoading;
  const cachedAt = fast?.cached ? fast.cachedAt : undefined;

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={t('dashboard.desc')}
          actions={
            <Button
              iconName="refresh"
              loading={loading}
              onClick={() => void load(range, dim, true)}
            >
              {t('dashboard.refresh')}
            </Button>
          }
        >
          {t('dashboard.title')}
        </Header>
      }
    >
      <SpaceBetween size="l">
        {/* One filter row above everything it scopes — never per-card. */}
        <SpaceBetween direction="horizontal" size="m">
          <FormField label={t('dashboard.filter.range')}>
            <div style={{ minWidth: 160 }}>
              <Select
                selectedOption={{ value: range, label: t(`dashboard.range.${range}`) }}
                onChange={({ detail }) => setRange(detail.selectedOption.value as DashboardRange)}
                options={RANGES.map((r) => ({ value: r, label: t(`dashboard.range.${r}`) }))}
              />
            </div>
          </FormField>
          <FormField
            label={t('dashboard.filter.dim')}
            description={t('dashboard.filter.dimHint')}
          >
            <div style={{ minWidth: 200 }}>
              <Select
                selectedOption={{ value: dim, label: t(`dashboard.dim.${dim}`) }}
                onChange={({ detail }) => setDim(detail.selectedOption.value as DashboardDim)}
                options={DIMS.map((d) => ({ value: d, label: t(`dashboard.dim.${d}`) }))}
              />
            </div>
          </FormField>
        </SpaceBetween>

        {error && (
          <Alert type="error" dismissible onDismiss={() => setError('')}>
            {error}
          </Alert>
        )}

        {cachedAt && (
          <Box variant="small" color="text-body-secondary">
            {t('dashboard.cachedAt').replace('{time}', cachedAt.slice(0, 19))}
          </Box>
        )}

        {/* Hold the prior render at reduced opacity while refetching. */}
        <div style={{ opacity: loading && fast ? 0.6 : 1, transition: 'opacity 120ms ease' }}>
          <SpaceBetween size="l">
            <KpiRow
              health={fast?.health}
              spans={spans?.spans}
              spansLoading={spansLoading}
              theme={theme}
            />

            {fast?.health && !fast.health.available && (
              <Alert type="warning">
                {t('dashboard.health.unavailable').replace('{reason}', fast.health.reason || '')}
              </Alert>
            )}

            <TokenCharts spans={spans?.spans} loading={spansLoading} dim={dim} theme={theme} />

            <BudgetMeter />

            <EvalPanel
              evaluations={fast?.evaluations}
              abComparison={fast?.abComparison}
              loading={fastLoading}
              theme={theme}
            />

            <ReleaseTable release={fast?.release} loading={fastLoading} />

            <SatisfactionCards theme={theme} />
          </SpaceBetween>
        </div>
      </SpaceBetween>
    </Container>
  );
}
