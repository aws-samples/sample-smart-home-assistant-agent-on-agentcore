import React, { useMemo } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Box from '@cloudscape-design/components/box';
import Table from '@cloudscape-design/components/table';
import SpaceBetween from '@cloudscape-design/components/space-between';
import LineChart from '@cloudscape-design/components/line-chart';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import { DashboardAbComparison, DashboardEvaluations, EvaluatorScore } from '../../api/adminApi';
import { ChartTheme, seriesColor, DE_EMPHASIS, MAX_SERIES } from './palette';
import { useI18n } from '../../i18n';

interface Props {
  evaluations?: DashboardEvaluations;
  abComparison?: DashboardAbComparison;
  loading: boolean;
  theme: ChartTheme;
}

/** Drift beyond this magnitude is called out. */
const DRIFT_THRESHOLD = 0.1;

/**
 * #4 Evaluation pass rate & drift.
 *
 * There are 11 evaluators — far past the ~7 colour classes that stay legible,
 * so the TABLE is the primary view and carries every one. The chart plots at
 * most three (the validated palette's hard cap), chosen as the evaluators with
 * the largest absolute drift, since drift is what the card is about.
 *
 * When one series is the story, the others are context: the largest-drift
 * evaluator keeps the accent hue and the rest fall back to the de-emphasis
 * grey. That is the "emphasis" form, not categorical colouring.
 */
export function EvalPanel({ evaluations, abComparison, loading, theme }: Props) {
  const { t } = useI18n();
  const evaluators = evaluations?.evaluators ?? [];

  // Ratio-scaled evaluators only: mixing a 0-1 metric with the Numerical
  // smarthome_SmartHomeQuality (mean ~3.47) on one axis would need a second
  // y-scale, and a dual-axis chart invents correlations that aren't there.
  const plotted = useMemo(() => {
    return [...evaluators]
      .filter((e) => e.isRatio && e.values.length > 1)
      .sort((a, b) => Math.abs(b.drift ?? 0) - Math.abs(a.drift ?? 0))
      .slice(0, MAX_SERIES);
  }, [evaluators]);

  const driftLeader = plotted[0];

  const chartSeries = plotted.map((e) => ({
    title: e.name.replace('Builtin.', ''),
    type: 'line' as const,
    // Emphasis: only the drift leader wears the accent.
    color: e.name === driftLeader?.name ? seriesColor(theme, 0) : DE_EMPHASIS[theme],
    data: e.timestamps.map((ts, i) => ({ x: ts.slice(5, 10), y: e.values[i] })),
    valueFormatter: (v: number) => v.toFixed(3),
  }));

  const driftStatus = (e: EvaluatorScore) => {
    if (e.drift === null) return <Box color="text-body-secondary">--</Box>;
    const big = Math.abs(e.drift) >= DRIFT_THRESHOLD;
    const sign = e.drift > 0 ? '+' : '';
    const label = `${sign}${e.drift.toFixed(3)}`;
    if (!big) return <Box color="text-body-secondary">{label}</Box>;
    // Rising scores are good, falling are bad — icon + text, never colour alone.
    return (
      <StatusIndicator type={e.drift > 0 ? 'success' : 'warning'}>{label}</StatusIndicator>
    );
  };

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header variant="h3" description={t('dashboard.eval.desc')}>
            {t('dashboard.eval.title')}
          </Header>
        }
      >
        <SpaceBetween size="m">
          {plotted.length > 0 && (
            <>
              <Box variant="small" color="text-body-secondary">
                {t('dashboard.eval.chartNote').replace('{n}', String(plotted.length))}
              </Box>
              <LineChart
                height={240}
                hideFilter
                statusType={loading && !evaluations ? 'loading' : 'finished'}
                loadingText={t('dashboard.loading')}
                series={chartSeries}
                xScaleType="categorical"
                xTitle={t('dashboard.token.xDay')}
                yTitle={t('dashboard.eval.score')}
                ariaLabel={t('dashboard.eval.title')}
                yTickFormatter={(v: number) => v.toFixed(1)}
                empty={
                  <Box textAlign="center" padding="m" color="text-body-secondary">
                    {t('dashboard.noData')}
                  </Box>
                }
              />
            </>
          )}

          <Table
            variant="embedded"
            loading={loading && !evaluations}
            loadingText={t('dashboard.loading')}
            items={evaluators}
            trackBy="name"
            columnDefinitions={[
              {
                id: 'name',
                header: t('dashboard.eval.evaluator'),
                cell: (e) => (
                  <SpaceBetween direction="horizontal" size="xs">
                    <span>{e.name}</span>
                    {!e.isRatio && <Badge color="blue">{t('dashboard.eval.numericalScale')}</Badge>}
                  </SpaceBetween>
                ),
              },
              {
                id: 'average',
                header: t('dashboard.eval.average'),
                cell: (e) => (e.isRatio ? `${(e.average * 100).toFixed(1)}%` : e.average.toFixed(2)),
              },
              {
                id: 'latest',
                header: t('dashboard.eval.latest'),
                cell: (e) => (e.isRatio ? `${(e.latest * 100).toFixed(1)}%` : e.latest.toFixed(2)),
              },
              { id: 'drift', header: t('dashboard.eval.drift'), cell: driftStatus },
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
          <Header variant="h3" description={t('dashboard.eval.abDesc')}>
            {t('dashboard.eval.abTitle')}
          </Header>
        }
      >
        {abComparison?.available && abComparison.rows?.length ? (
          <Table
            variant="embedded"
            items={abComparison.rows}
            columnDefinitions={[
              { id: 'variant', header: t('dashboard.eval.variant'), cell: (r) => r.variant },
              { id: 'evaluator', header: t('dashboard.eval.evaluator'), cell: (r) => r.evaluator },
              {
                id: 'average',
                header: t('dashboard.eval.average'),
                cell: (r) => `${(r.average * 100).toFixed(1)}%`,
              },
              { id: 'n', header: t('dashboard.eval.samples'), cell: (r) => r.n },
            ]}
          />
        ) : (
          // Deliberately an empty state, not an invented curve: the A/B configs
          // exist but their tests are STOPPED and emit no datapoints.
          <Alert type="info">{t('dashboard.eval.abEmpty')}</Alert>
        )}
      </Container>
    </SpaceBetween>
  );
}
