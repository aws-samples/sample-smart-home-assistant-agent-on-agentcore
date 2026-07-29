import React from 'react';
import Box from '@cloudscape-design/components/box';
import Container from '@cloudscape-design/components/container';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Popover from '@cloudscape-design/components/popover';
import Icon from '@cloudscape-design/components/icon';
import { DashboardHealth, DashboardSpans } from '../../api/adminApi';
import { ChartTheme, seriesColor, STATUS, errorRateSeverity } from './palette';
import { Sparkline } from './Sparkline';
import { useI18n } from '../../i18n';

interface Props {
  health?: DashboardHealth;
  spans?: DashboardSpans;
  spansLoading: boolean;
  theme: ChartTheme;
}

/** Compact large numbers: 1284 -> 1.3K, 683912 -> 684K. */
function compact(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '--';
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(digits)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(digits)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(digits)}K`;
  if (abs >= 10) return n.toFixed(0);
  return n.toFixed(digits);
}

function ms(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '--';
  return n >= 1000 ? `${(n / 1000).toFixed(2)}s` : `${n.toFixed(0)}ms`;
}

interface TileProps {
  label: string;
  /** Proportional figures (no tabular-nums): a big standalone number reads
   *  loose when every digit is forced to a zero's width. */
  value: string;
  secondary?: React.ReactNode;
  sparkValues?: number[];
  sparkColor?: string;
  hint?: string;
}

function StatTile({ label, value, secondary, sparkValues, sparkColor, hint }: TileProps) {
  return (
    <SpaceBetween size="xxs">
      <Box variant="awsui-key-label">
        {label}
        {hint && (
          <>
            {' '}
            <Popover
              dismissButton={false}
              position="top"
              size="small"
              triggerType="custom"
              content={<Box variant="p">{hint}</Box>}
            >
              <span style={{ cursor: 'help' }}>
                <Icon name="status-info" size="small" variant="subtle" />
              </span>
            </Popover>
          </>
        )}
      </Box>
      <Box fontSize="display-l" fontWeight="bold">{value}</Box>
      {secondary}
      {sparkValues && sparkValues.length > 1 && sparkColor && (
        <Sparkline values={sparkValues} color={sparkColor} ariaLabel={`${label} trend`} />
      )}
    </SpaceBetween>
  );
}

/**
 * #1 Real-time agent health.
 *
 * A KPI row of stat tiles, not a grouped bar chart: these are four unrelated
 * headline numbers, and the form heuristic sends "a handful of headline
 * numbers" to tiles. Error rate wears a reserved STATUS colour plus an icon
 * and text label, because it MEANS good/bad — colour never carries that alone.
 */
export function KpiRow({ health, spans, spansLoading, theme }: Props) {
  const { t } = useI18n();
  const accent = seriesColor(theme, 0);

  const errorRate = health?.errorRate ?? null;
  const severity = errorRate === null ? null : errorRateSeverity(errorRate);
  const severityType =
    severity === 'good' ? 'success'
      : severity === 'warning' ? 'warning'
        : severity === 'serious' ? 'warning'
          : severity === 'critical' ? 'error'
            : 'info';

  const invocationSeries = health?.series?.invocations?.values ?? [];
  const sessionSeries = health?.series?.sessions?.values ?? [];
  const ttftSeries = (spans?.trend ?? []).map((p) => p.ttftP95).filter((v) => v > 0);

  return (
    <Container>
      <ColumnLayout columns={4} variant="text-grid">
        <StatTile
          label={t('dashboard.kpi.activeSessions')}
          value={compact(health?.activeSessionsAccount, 0)}
          // ActiveSessionCount has no per-runtime dimension in CloudWatch, so
          // this figure covers every AgentCore runtime in the account.
          hint={t('dashboard.kpi.activeSessionsHint')}
          secondary={
            <Box variant="small" color="text-body-secondary">
              {t('dashboard.kpi.sessionsInWindow').replace('{n}', compact(health?.sessions, 0))}
            </Box>
          }
          sparkValues={sessionSeries}
          sparkColor={accent}
        />

        <StatTile
          label={t('dashboard.kpi.ttft')}
          value={spansLoading && !spans ? '…' : ms(spans?.totals?.ttftP95Ms)}
          hint={t('dashboard.kpi.ttftHint')}
          secondary={
            <Box variant="small" color="text-body-secondary">
              {`P99 ${spansLoading && !spans ? '…' : ms(spans?.totals?.ttftP99Ms)}`}
            </Box>
          }
          sparkValues={ttftSeries}
          sparkColor={accent}
        />

        <StatTile
          label={t('dashboard.kpi.errorRate')}
          value={errorRate === null ? '--' : `${(errorRate * 100).toFixed(1)}%`}
          hint={t('dashboard.kpi.errorRateHint')}
          secondary={
            errorRate === null ? (
              <Box variant="small" color="text-body-secondary">{t('dashboard.kpi.noTraffic')}</Box>
            ) : (
              // Icon + label so severity never depends on hue alone.
              <StatusIndicator type={severityType as any}>
                {t(`dashboard.severity.${severity}`)}
              </StatusIndicator>
            )
          }
        />

        <StatTile
          label={t('dashboard.kpi.qps')}
          value={health?.qps ? health.qps.toFixed(4) : '0'}
          hint={t('dashboard.kpi.qpsHint')}
          secondary={
            <Box variant="small" color="text-body-secondary">
              {t('dashboard.kpi.invocations').replace('{n}', compact(health?.invocations, 0))}
            </Box>
          }
          sparkValues={invocationSeries}
          sparkColor={accent}
        />
      </ColumnLayout>
    </Container>
  );
}

export { compact, ms };
