import React from 'react';
import BarChart from '@cloudscape-design/components/bar-chart';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import { DashboardDim, DashboardSpans } from '../../api/adminApi';
import { ChartTheme, seriesColor } from './palette';
import { ChartTableToggle } from './ChartTableToggle';
import { compact } from './KpiRow';
import { useI18n } from '../../i18n';

interface Props {
  spans?: DashboardSpans;
  loading: boolean;
  dim: DashboardDim;
  theme: ChartTheme;
}

/**
 * #2 Token cost trend + attribution.
 *
 * Stacked columns for the trend: this is part-to-whole over time (input vs
 * output of one total). Note output tokens are genuinely tiny next to input
 * (measured ~28:1), so the output segment renders as a thin sliver — that is
 * the real shape of the data, not a defect, and the tooltip plus the table
 * view carry the exact numbers.
 *
 * Attribution is a HORIZONTAL bar chart because the categories are long
 * strings (emails, model ids). It is one series, so it takes slot 1 for every
 * bar and ships no legend box — colouring bars by their own value would spend
 * the identity channel re-encoding what bar length already shows.
 */
export function TokenCharts({ spans, loading, dim, theme }: Props) {
  const { t } = useI18n();
  const inputColor = seriesColor(theme, 0);
  const outputColor = seriesColor(theme, 1);

  const trend = spans?.trend ?? [];
  const attribution = spans?.attribution ?? [];

  const dayLabel = (raw: string) => (raw ? raw.slice(5, 10) : '');

  const trendChart = (
    <BarChart
      stackedBars
      height={240}
      hideFilter
      statusType={loading && !spans ? 'loading' : 'finished'}
      loadingText={t('dashboard.loading')}
      series={[
        {
          title: t('dashboard.token.input'),
          type: 'bar',
          color: inputColor,
          data: trend.map((p) => ({ x: dayLabel(p.day), y: p.inputTokens })),
          valueFormatter: (v: number) => compact(v, 1),
        },
        {
          title: t('dashboard.token.output'),
          type: 'bar',
          color: outputColor,
          data: trend.map((p) => ({ x: dayLabel(p.day), y: p.outputTokens })),
          valueFormatter: (v: number) => compact(v, 1),
        },
      ]}
      xTitle={t('dashboard.token.xDay')}
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
  );

  const attributionChart = (
    <BarChart
      horizontalBars
      height={Math.max(180, attribution.length * 34)}
      hideFilter
      hideLegend
      statusType={loading && !spans ? 'loading' : 'finished'}
      loadingText={t('dashboard.loading')}
      series={[
        {
          title: t('dashboard.token.totalTokens'),
          type: 'bar',
          color: inputColor,
          data: attribution.map((r) => ({
            x: r.key,
            y: r.inputTokens + r.outputTokens,
          })),
          valueFormatter: (v: number) => compact(v, 1),
        },
      ]}
      xTitle={t(`dashboard.dim.${dim}`)}
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
  );

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h3"
            description={t('dashboard.token.trendDesc')}
            actions={loading && spans ? <Spinner /> : undefined}
          >
            {t('dashboard.token.trendTitle')}
          </Header>
        }
      >
        <ChartTableToggle
          chart={trendChart}
          items={trend}
          columns={[
            { id: 'day', header: t('dashboard.token.xDay'), cell: (p) => p.day.slice(0, 10) },
            { id: 'in', header: t('dashboard.token.input'), cell: (p) => p.inputTokens.toLocaleString() },
            { id: 'out', header: t('dashboard.token.output'), cell: (p) => p.outputTokens.toLocaleString() },
            { id: 'p95', header: 'TTFT P95', cell: (p) => `${p.ttftP95.toFixed(0)}ms` },
            { id: 'n', header: t('dashboard.token.calls'), cell: (p) => p.n },
          ]}
        />
      </Container>

      <Container
        header={
          <Header variant="h3" description={t(`dashboard.token.attributionDesc.${dim}`)}>
            {t('dashboard.token.attributionTitle')}
          </Header>
        }
      >
        <ChartTableToggle
          chart={attributionChart}
          items={attribution}
          columns={[
            { id: 'key', header: t(`dashboard.dim.${dim}`), cell: (r) => r.key },
            { id: 'in', header: t('dashboard.token.input'), cell: (r) => r.inputTokens.toLocaleString() },
            { id: 'out', header: t('dashboard.token.output'), cell: (r) => r.outputTokens.toLocaleString() },
            { id: 'sessions', header: t('dashboard.token.sessions'), cell: (r) => r.sessions },
          ]}
        />
      </Container>
    </SpaceBetween>
  );
}
