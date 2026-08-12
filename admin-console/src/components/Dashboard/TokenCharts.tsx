import React from 'react';
import BarChart from '@cloudscape-design/components/bar-chart';
import Box from '@cloudscape-design/components/box';
import { DashboardDim, DashboardSpans } from '../../api/adminApi';
import { ChartTheme, seriesColor } from './palette';
import { ChartTableToggle } from './ChartTableToggle';
import { compact, dayTick } from './format';
import { useI18n } from '../../i18n';

interface Props {
  spans?: DashboardSpans;
  loading: boolean;
  theme: ChartTheme;
  chartHeight: number;
}

/** Attribution additionally needs the dimension, to label its category axis. */
interface AttributionProps extends Props {
  dim: DashboardDim;
}

/**
 * #2 Token trend — GROUPED columns: input and output get a bar each, side by side.
 *
 * Stacked was the obvious choice (part-to-whole over time) and it failed on this
 * data. Measured input:output is ~28:1, so the output segment was a 3px sliver on
 * top of the input bar — present, but impossible to read a trend from, and easy to
 * mistake for a rendering artefact. Stacking also puts output's baseline on top of
 * input, so its own day-to-day movement is not comparable by eye at all.
 *
 * Grouping gives each series its own baseline. Output is still short beside input
 * — that IS the data — but it is a bar you can follow across days rather than a
 * line on top of another bar. The total is what stacking bought and it is the one
 * thing lost; it was never the point of this panel, and the table twin has it.
 */
export function TokenTrend({ spans, loading, theme, chartHeight }: Props) {
  const { t } = useI18n();
  const trend = spans?.trend ?? [];

  // Where span history begins, when the selected window reaches back past it.
  // Noted rather than zero-filled: the pre-horizon days hold no telemetry, and a
  // bar of height zero would claim they held no traffic. The value is probed per
  // request because it moves — `aws/spans` keeps 30 rolling days.
  const horizon = spans?.dataFrom
    ? new Date(spans.dataFrom)
    : null;
  const firstDay = trend.length ? new Date(trend[0].day) : null;
  const showHorizon = Boolean(
    horizon && firstDay && horizon.getTime() > firstDay.getTime() - 86400_000,
  );

  return (
    <ChartTableToggle
      footer={
        showHorizon ? (
          <Box variant="small" color="text-body-secondary">
            {t('dashboard.token.dataFrom').replace(
              '{date}', spans!.dataFrom!.slice(0, 10))}
          </Box>
        ) : undefined
      }
      chart={
        <BarChart
          height={chartHeight}
          hideFilter
          statusType={loading && !spans ? 'loading' : 'finished'}
          loadingText={t('dashboard.loading')}
          series={[
            {
              title: t('dashboard.token.input'),
              type: 'bar',
              color: seriesColor(theme, 0),
              data: trend.map((p) => ({ x: dayTick(p.day), y: p.inputTokens })),
              valueFormatter: (v: number) => compact(v, 1),
            },
            {
              title: t('dashboard.token.output'),
              type: 'bar',
              color: seriesColor(theme, 1),
              data: trend.map((p) => ({ x: dayTick(p.day), y: p.outputTokens })),
              valueFormatter: (v: number) => compact(v, 1),
            },
          ]}
          yTitle={t('dashboard.token.yTokens')}
          ariaLabel={t('dashboard.token.trendTitle')}
          xScaleType="categorical"
          yTickFormatter={(v: number) => compact(v, 0)}
          empty={
            <Box textAlign="center" padding="m" color="text-body-secondary">
              {t('dashboard.noData')}
            </Box>
          }
        />
      }
      items={trend}
      columns={[
        { id: 'day', header: t('dashboard.token.xDay'), cell: (p) => p.day.slice(0, 10) },
        { id: 'in', header: t('dashboard.token.input'), cell: (p) => p.inputTokens.toLocaleString() },
        { id: 'out', header: t('dashboard.token.output'), cell: (p) => p.outputTokens.toLocaleString() },
        { id: 'p95', header: 'TTFT P95', cell: (p) => `${p.ttftP95.toFixed(0)}ms` },
        { id: 'n', header: t('dashboard.token.calls'), cell: (p) => p.n },
      ]}
    />
  );
}

/**
 * #2b Token attribution — horizontal bars, because the categories are long
 * strings (emails, model ids).
 *
 * ONE series, so every bar takes slot 1 and there is no legend box. Colouring
 * bars by their own value would spend the identity channel re-encoding what
 * bar length already shows.
 */
export function TokenAttribution({ spans, loading, dim, theme, chartHeight }: AttributionProps) {
  const { t } = useI18n();
  const attribution = spans?.attribution ?? [];

  return (
    <ChartTableToggle
      chart={
        <BarChart
          horizontalBars
          height={chartHeight}
          hideFilter
          hideLegend
          statusType={loading && !spans ? 'loading' : 'finished'}
          loadingText={t('dashboard.loading')}
          series={[
            {
              title: t('dashboard.token.totalTokens'),
              type: 'bar',
              color: seriesColor(theme, 0),
              data: attribution.map((r) => ({
                x: r.key,
                y: r.inputTokens + r.outputTokens,
              })),
              valueFormatter: (v: number) => compact(v, 1),
            },
          ]}
          yTitle={t('dashboard.token.yTokens')}
          ariaLabel={t('dashboard.token.attributionTitle')}
          xScaleType="categorical"
          yTickFormatter={(v: number) => compact(v, 0)}
          empty={
            <Box textAlign="center" padding="m" color="text-body-secondary">
              {t('dashboard.noData')}
            </Box>
          }
        />
      }
      items={attribution}
      columns={[
        { id: 'key', header: t(`dashboard.dim.${dim}`), cell: (r) => r.key },
        { id: 'in', header: t('dashboard.token.input'), cell: (r) => r.inputTokens.toLocaleString() },
        { id: 'out', header: t('dashboard.token.output'), cell: (r) => r.outputTokens.toLocaleString() },
        { id: 'sessions', header: t('dashboard.token.sessions'), cell: (r) => r.sessions },
      ]}
    />
  );
}
