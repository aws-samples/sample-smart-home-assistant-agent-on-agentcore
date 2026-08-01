import React from 'react';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import LineChart from '@cloudscape-design/components/line-chart';
import { MOCK_SATISFACTION } from './mockData';
import { ChartTheme, seriesColor } from './palette';
import { ChartTableToggle } from './ChartTableToggle';
import { useI18n } from '../../i18n';

interface Props {
  theme: ChartTheme;
  chartHeight: number;
}

/**
 * #6 User satisfaction — SIMULATED. Panel body.
 *
 * Three figures plus a SINGLE-series line for escalation rate — one series
 * needs no legend box, since the panel title already names what is plotted.
 * Two-part shares (thumbs up/down) are a ratio figure, never a pie.
 *
 * No real source: the chatbot ships no thumbs up/down control, and the
 * user-feedback skill only writes JSON into the runtime's
 * /mnt/workspace/feedback/ — readable via Remote Shell, not aggregatable. The
 * nearest real proxies are Helpfulness / GoalSuccessRate in the evaluation
 * panel, which are NOT CSAT.
 */
export function SatisfactionCards({ theme, chartHeight }: Props) {
  const { t } = useI18n();
  const s = MOCK_SATISFACTION;
  const totalVotes = s.thumbsUp + s.thumbsDown;
  const upPct = totalVotes ? (s.thumbsUp / totalVotes) * 100 : 0;
  const latest = s.escalationTrend[s.escalationTrend.length - 1].rate;

  return (
    <SpaceBetween size="s">
      <SpaceBetween direction="horizontal" size="l">
        <div>
          <Box variant="awsui-key-label">{t('dashboard.satisfaction.csat')}</Box>
          <Box fontSize="heading-xl" fontWeight="bold">
            {s.csat.toFixed(1)}
            <Box variant="span" color="text-body-secondary" fontSize="body-m" fontWeight="normal">
              {` / ${s.csatScale}`}
            </Box>
          </Box>
        </div>
        <div>
          <Box variant="awsui-key-label">{t('dashboard.satisfaction.thumbs')}</Box>
          <Box fontSize="heading-xl" fontWeight="bold">{`${upPct.toFixed(0)}%`}</Box>
          <Box variant="small" color="text-body-secondary">{`${s.thumbsUp} / ${s.thumbsDown}`}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">{t('dashboard.satisfaction.latestEscalation')}</Box>
          <Box fontSize="heading-xl" fontWeight="bold">{`${(latest * 100).toFixed(1)}%`}</Box>
        </div>
      </SpaceBetween>

      <ChartTableToggle
        chart={
          <LineChart
            height={chartHeight}
            hideFilter
            hideLegend
            series={[
              {
                title: t('dashboard.satisfaction.escalationRate'),
                type: 'line',
                color: seriesColor(theme, 0),
                data: s.escalationTrend.map((p) => ({ x: p.day.slice(5), y: p.rate * 100 })),
                valueFormatter: (v: number) => `${v.toFixed(1)}%`,
              },
            ]}
            xScaleType="categorical"
            yTitle="%"
            ariaLabel={t('dashboard.satisfaction.escalationRate')}
            yTickFormatter={(v: number) => `${v.toFixed(0)}%`}
          />
        }
        items={s.escalationTrend}
        columns={[
          { id: 'day', header: t('dashboard.token.xDay'), cell: (p) => p.day },
          {
            id: 'rate',
            header: t('dashboard.satisfaction.escalationRate'),
            cell: (p) => `${(p.rate * 100).toFixed(1)}%`,
          },
        ]}
      />

      <Box variant="small" color="text-body-secondary">
        {t('dashboard.satisfaction.footnote')}
      </Box>
    </SpaceBetween>
  );
}
