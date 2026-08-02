import React from 'react';
import Box from '@cloudscape-design/components/box';
import Grid from '@cloudscape-design/components/grid';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Popover from '@cloudscape-design/components/popover';
import Icon from '@cloudscape-design/components/icon';
import Badge from '@cloudscape-design/components/badge';
import { DashboardFastResponse, DashboardSpans } from '../../api/adminApi';
import { ChartTheme, seriesColor, errorRateSeverity } from './palette';
import { Sparkline } from './Sparkline';
import { compact, ms, pct } from './format';
import { useI18n } from '../../i18n';

interface Props {
  fast?: DashboardFastResponse;
  spans?: DashboardSpans;
  spansLoading: boolean;
  theme: ChartTheme;
}

type Sev = 'good' | 'warning' | 'serious' | 'critical';

const SEV_TO_INDICATOR: Record<Sev, 'success' | 'warning' | 'error'> = {
  good: 'success',
  warning: 'warning',
  serious: 'warning',
  critical: 'error',
};

interface TileProps {
  label: string;
  value: string;
  unit?: string;
  sub?: React.ReactNode;
  spark?: number[];
  sparkColor?: string;
  hint?: string;
}

/**
 * One cell of the status strip. Deliberately terse: label, one big number,
 * one line of context, optional sparkline. Anything longer belongs in a panel
 * below, not in the strip an operator scans first.
 */
function Tile({ label, value, unit, sub, spark, sparkColor, hint }: TileProps) {
  return (
    <div style={{ minWidth: 0 }}>
      <SpaceBetween size="xxxs">
        <Box variant="awsui-key-label">
          {label}
          {hint && (
            <>
              {' '}
              <Popover
                dismissButton={false}
                position="bottom"
                size="medium"
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
        <Box fontSize="display-l" fontWeight="bold">
          {value}
          {unit && (
            <Box variant="span" color="text-body-secondary" fontSize="heading-s" fontWeight="normal">
              {` ${unit}`}
            </Box>
          )}
        </Box>
        <div style={{ minHeight: 20 }}>{sub}</div>
        <div style={{ height: 28 }}>
          {spark && spark.length > 1 && sparkColor && (
            <Sparkline values={spark} color={sparkColor} ariaLabel={`${label} trend`} />
          )}
        </div>
      </SpaceBetween>
    </div>
  );
}

/**
 * The status strip — the band an ops wall leads with.
 *
 * Six live signals in one row so an operator answers "is anything wrong?"
 * without scrolling. Health, latency, errors and throughput sit beside the
 * two governance signals (active version, evaluation quality) because on a
 * monitoring wall "what's deployed" is as operational as "how fast is it".
 *
 * Error rate and the overall verdict wear reserved STATUS colours plus an icon
 * and text label — colour never carries the meaning alone.
 */
export function StatusStrip({ fast, spans, spansLoading, theme }: Props) {
  const { t } = useI18n();
  const accent = seriesColor(theme, 0);
  const h = fast?.health;
  const rel = fast?.release;
  const evals = fast?.evaluations?.evaluators ?? [];

  const errorRate = h?.errorRate ?? null;
  const errSev: Sev | null = errorRate === null ? null : errorRateSeverity(errorRate);

  // Headline quality = mean of the ratio-scaled evaluators. The Numerical-scale
  // evaluator is excluded rather than averaged into a 0-1 figure.
  const ratioEvals = evals.filter((e) => e.isRatio);
  const qualityMean = ratioEvals.length
    ? ratioEvals.reduce((s, e) => s + e.average, 0) / ratioEvals.length
    : null;
  const driftAlerts = ratioEvals.filter((e) => (e.drift ?? 0) <= -0.1).length;

  return (
    <Grid
      gridDefinition={[
        { colspan: { default: 12, xs: 6, s: 4, m: 2 } },
        { colspan: { default: 12, xs: 6, s: 4, m: 2 } },
        { colspan: { default: 12, xs: 6, s: 4, m: 2 } },
        { colspan: { default: 12, xs: 6, s: 4, m: 2 } },
        { colspan: { default: 12, xs: 6, s: 4, m: 2 } },
        { colspan: { default: 12, xs: 6, s: 4, m: 2 } },
      ]}
    >
      <Tile
        label={t('dashboard.kpi.activeSessions')}
        value={compact(h?.activeSessionsAccount, 0)}
        hint={t('dashboard.kpi.activeSessionsHint')}
        sub={
          <Box variant="small" color="text-body-secondary">
            {t('dashboard.kpi.sessionsInWindow').replace('{n}', compact(h?.sessions, 0))}
          </Box>
        }
        spark={h?.series?.sessions?.values}
        sparkColor={accent}
      />

      <Tile
        label={t('dashboard.kpi.ttft')}
        value={spansLoading && !spans ? '…' : ms(spans?.totals?.ttftP95Ms)}
        hint={t('dashboard.kpi.ttftHint')}
        sub={
          <Box variant="small" color="text-body-secondary">
            {`P99 ${spansLoading && !spans ? '…' : ms(spans?.totals?.ttftP99Ms)}`}
          </Box>
        }
        spark={(spans?.trend ?? []).map((p) => p.ttftP95).filter((v) => v > 0)}
        sparkColor={accent}
      />

      <Tile
        label={t('dashboard.kpi.errorRate')}
        value={pct(errorRate)}
        hint={t('dashboard.kpi.errorRateHint')}
        sub={
          errSev ? (
            <StatusIndicator type={SEV_TO_INDICATOR[errSev]}>
              {t(`dashboard.severity.${errSev}`)}
            </StatusIndicator>
          ) : (
            <Box variant="small" color="text-body-secondary">
              {t('dashboard.kpi.noTraffic')}
            </Box>
          )
        }
      />

      <Tile
        label={t('dashboard.kpi.qps')}
        value={h?.qps ? h.qps.toFixed(4) : '0'}
        hint={t('dashboard.kpi.qpsHint')}
        sub={
          <Box variant="small" color="text-body-secondary">
            {t('dashboard.kpi.invocations').replace('{n}', compact(h?.invocations, 0))}
          </Box>
        }
        spark={h?.series?.invocations?.values}
        sparkColor={accent}
      />

      <Tile
        label={t('dashboard.strip.tokens')}
        value={spansLoading && !spans ? '…' : compact(
          (spans?.totals?.inputTokens ?? 0) + (spans?.totals?.outputTokens ?? 0), 1,
        )}
        hint={t('dashboard.strip.tokensHint')}
        sub={
          <Box variant="small" color="text-body-secondary">
            {`${t('dashboard.token.input')} ${compact(spans?.totals?.inputTokens, 1)} · ${t('dashboard.token.output')} ${compact(spans?.totals?.outputTokens, 1)}`}
          </Box>
        }
      />

      <Tile
        label={t('dashboard.strip.quality')}
        value={qualityMean === null ? '--' : pct(qualityMean, 0)}
        hint={t('dashboard.strip.qualityHint')}
        sub={
          driftAlerts > 0 ? (
            <StatusIndicator type="warning">
              {t('dashboard.strip.driftAlerts').replace('{n}', String(driftAlerts))}
            </StatusIndicator>
          ) : rel?.latestVersion ? (
            <SpaceBetween direction="horizontal" size="xxs">
              <Box variant="small" color="text-body-secondary">
                {t('dashboard.strip.version')}
              </Box>
              <Badge color={rel.abRunning ? 'blue' : 'green'}>{`v${rel.latestVersion}`}</Badge>
            </SpaceBetween>
          ) : (
            <Box variant="small" color="text-body-secondary">
              {t('dashboard.strip.noEvalData')}
            </Box>
          )
        }
      />
    </Grid>
  );
}
