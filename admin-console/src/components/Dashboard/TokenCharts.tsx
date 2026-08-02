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
  dim: DashboardDim;
  theme: ChartTheme;
  chartHeight: number;
}

/**
 * #2 Token trend — stacked columns (part-to-whole over time).
 *
 * Output tokens are genuinely tiny beside input (measured ~28:1), so the output
 * segment renders as a thin sliver. That is the real shape of the data, not a
 * defect; the tooltip and the table twin carry the exact numbers.
 */
export function TokenTrend({ spans, loading, theme, chartHeight }: Props) {
  const { t } = useI18n();
  const trend = spans?.trend ?? [];

  return (
    <ChartTableToggle
      chart={
        <BarChart
          stackedBars
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
export function TokenAttribution({ spans, loading, dim, theme, chartHeight }: Props) {
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
