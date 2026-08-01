import React, { useMemo } from 'react';
import Box from '@cloudscape-design/components/box';
import Table from '@cloudscape-design/components/table';
import SpaceBetween from '@cloudscape-design/components/space-between';
import LineChart from '@cloudscape-design/components/line-chart';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Badge from '@cloudscape-design/components/badge';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Popover from '@cloudscape-design/components/popover';
import Icon from '@cloudscape-design/components/icon';
import { DashboardAbComparison, DashboardEvaluations, EvaluatorScore } from '../../api/adminApi';
import { ChartTheme, seriesColor, DE_EMPHASIS, MAX_SERIES } from './palette';
import { dayTick, pct } from './format';
import { useI18n } from '../../i18n';

interface Props {
  evaluations?: DashboardEvaluations;
  abComparison?: DashboardAbComparison;
  loading: boolean;
  theme: ChartTheme;
  chartHeight: number;
}

/** Drift beyond this magnitude is called out. */
const DRIFT_THRESHOLD = 0.1;

/**
 * #4 Evaluation quality & drift — panel body (the wall supplies the frame).
 *
 * 11 evaluators is far past the ~7 colour classes that stay legible, so the
 * TABLE carries all of them and the chart plots at most three — the validated
 * palette's hard cap — chosen by largest absolute drift, which is what the
 * panel is about.
 *
 * The drift leader keeps the accent hue and the rest fall back to the
 * de-emphasis grey: that is the "emphasis" form, not categorical colouring.
 * The A/B comparison rides along as a collapsed section rather than its own
 * card, so an empty A/B state costs no wall space.
 */
export function EvalPanel({ evaluations, abComparison, loading, theme, chartHeight }: Props) {
  const { t } = useI18n();
  const evaluators = evaluations?.evaluators ?? [];

  // Ratio-scaled only: plotting the Numerical-scale evaluator (mean ~3.47)
  // against 0-1 metrics would need a second y-axis, and a dual-axis chart
  // invents correlations that aren't in the data.
  const plotted = useMemo(
    () =>
      [...evaluators]
        .filter((e) => e.isRatio && e.values.length > 1)
        .sort((a, b) => Math.abs(b.drift ?? 0) - Math.abs(a.drift ?? 0))
        .slice(0, MAX_SERIES),
    [evaluators],
  );
  const driftLeader = plotted[0];

  const driftCell = (e: EvaluatorScore) => {
    if (e.drift === null) return <Box color="text-body-secondary">--</Box>;
    const label = `${e.drift > 0 ? '+' : ''}${e.drift.toFixed(3)}`;
    if (Math.abs(e.drift) < DRIFT_THRESHOLD) {
      return <Box color="text-body-secondary">{label}</Box>;
    }
    // Rising is good, falling is bad — icon + text, never colour alone.
    return <StatusIndicator type={e.drift > 0 ? 'success' : 'warning'}>{label}</StatusIndicator>;
  };

  return (
    <SpaceBetween size="s">
      {plotted.length > 0 && (
        <LineChart
          height={chartHeight}
          hideFilter
          statusType={loading && !evaluations ? 'loading' : 'finished'}
          loadingText={t('dashboard.loading')}
          series={plotted.map((e) => ({
            title: e.name.replace('Builtin.', ''),
            type: 'line' as const,
            color: e.name === driftLeader?.name ? seriesColor(theme, 0) : DE_EMPHASIS[theme],
            data: e.timestamps.map((ts, i) => ({ x: dayTick(ts), y: e.values[i] })),
            valueFormatter: (v: number) => v.toFixed(3),
          }))}
          xScaleType="categorical"
          yTitle={t('dashboard.eval.score')}
          ariaLabel={t('dashboard.eval.title')}
          yTickFormatter={(v: number) => v.toFixed(1)}
          empty={
            <Box textAlign="center" padding="m" color="text-body-secondary">
              {t('dashboard.noData')}
            </Box>
          }
        />
      )}

      <Table
        variant="embedded"
        contentDensity="compact"
        loading={loading && !evaluations}
        loadingText={t('dashboard.loading')}
        items={evaluators}
        trackBy="name"
        columnDefinitions={[
          {
            id: 'name',
            header: t('dashboard.eval.evaluator'),
            cell: (e) => (
              <SpaceBetween direction="horizontal" size="xxs">
                <span>{e.name.replace('Builtin.', '')}</span>
                {!e.isRatio && <Badge color="blue">{t('dashboard.eval.numericalScale')}</Badge>}
              </SpaceBetween>
            ),
          },
          {
            id: 'average',
            header: t('dashboard.eval.average'),
            cell: (e) => (e.isRatio ? pct(e.average) : e.average.toFixed(2)),
          },
          {
            id: 'latest',
            header: t('dashboard.eval.latest'),
            cell: (e) => (e.isRatio ? pct(e.latest) : e.latest.toFixed(2)),
          },
          {
            // Header stays one word: at 6 grid columns the cell is ~477px, and a
            // spelled-out "(2nd half - 1st half)" pushed the table to 651px and
            // clipped this column. The definition lives in the popover instead.
            id: 'drift',
            header: (
              <SpaceBetween direction="horizontal" size="xxs">
                <span>{t('dashboard.eval.drift')}</span>
                <Popover
                  dismissButton={false}
                  position="left"
                  size="medium"
                  triggerType="custom"
                  content={<Box variant="p">{t('dashboard.eval.driftHint')}</Box>}
                >
                  <span style={{ cursor: 'help' }}>
                    <Icon name="status-info" size="small" variant="subtle" />
                  </span>
                </Popover>
              </SpaceBetween>
            ),
            cell: driftCell,
          },
        ]}
        empty={
          <Box textAlign="center" padding="m" color="text-body-secondary">
            {t('dashboard.noData')}
          </Box>
        }
      />

      {/* Folded away by default — its configs are ACTIVE but the tests are
          STOPPED, so an expanded empty card would waste prime wall space. */}
      <ExpandableSection
        headerText={t('dashboard.eval.abTitle')}
        variant="footer"
        headerCounter={abComparison?.rows?.length ? `(${abComparison.rows.length})` : undefined}
      >
        {abComparison?.available && abComparison.rows?.length ? (
          <Table
            variant="embedded"
            contentDensity="compact"
            items={abComparison.rows}
            columnDefinitions={[
              { id: 'variant', header: t('dashboard.eval.variant'), cell: (r) => r.variant },
              { id: 'evaluator', header: t('dashboard.eval.evaluator'), cell: (r) => r.evaluator.replace('Builtin.', '') },
              { id: 'average', header: t('dashboard.eval.average'), cell: (r) => pct(r.average) },
              { id: 'n', header: t('dashboard.eval.samples'), cell: (r) => r.n },
            ]}
          />
        ) : (
          <Box variant="p" color="text-body-secondary">
            {t('dashboard.eval.abEmpty')}
          </Box>
        )}
      </ExpandableSection>
    </SpaceBetween>
  );
}
