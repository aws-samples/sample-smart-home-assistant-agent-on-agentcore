import React from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Box from '@cloudscape-design/components/box';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import SpaceBetween from '@cloudscape-design/components/space-between';
import LineChart from '@cloudscape-design/components/line-chart';
import { MOCK_SATISFACTION, MOCK_PROVENANCE } from './mockData';
import { ChartTheme, seriesColor } from './palette';
import { DemoDataBadge } from './DemoDataBadge';
import { ChartTableToggle } from './ChartTableToggle';
import { useI18n } from '../../i18n';

interface Props {
  theme: ChartTheme;
}

/**
 * #6 User satisfaction — SIMULATED.
 *
 * Stat tiles for CSAT and the thumbs ratio (two-part shares never get a pie),
 * plus a SINGLE-series line for escalation rate — one series needs no legend
 * box, since the card title already names what is plotted.
 *
 * No real source: the chatbot ships no thumbs up/down control, and the
 * `user-feedback` skill only writes JSON files into the runtime's
 * /mnt/workspace/feedback/ which are readable via Remote Shell but not
 * aggregatable. The nearest real proxies are the online evaluators'
 * Helpfulness / GoalSuccessRate in the Evaluation card, which are NOT CSAT.
 */
export function SatisfactionCards({ theme }: Props) {
  const { t } = useI18n();
  const accent = seriesColor(theme, 0);
  const s = MOCK_SATISFACTION;
  const totalVotes = s.thumbsUp + s.thumbsDown;
  const upPct = totalVotes ? (s.thumbsUp / totalVotes) * 100 : 0;

  const escalationChart = (
    <LineChart
      height={200}
      hideFilter
      hideLegend
      series={[
        {
          title: t('dashboard.satisfaction.escalationRate'),
          type: 'line',
          color: accent,
          data: s.escalationTrend.map((p) => ({ x: p.day.slice(5), y: p.rate * 100 })),
          valueFormatter: (v: number) => `${v.toFixed(1)}%`,
        },
      ]}
      xScaleType="categorical"
      xTitle={t('dashboard.token.xDay')}
      yTitle="%"
      ariaLabel={t('dashboard.satisfaction.escalationRate')}
      yTickFormatter={(v: number) => `${v.toFixed(0)}%`}
    />
  );

  return (
    <Container
      header={
        <Header
          variant="h3"
          description={t('dashboard.satisfaction.desc')}
          actions={<DemoDataBadge provenanceKey={MOCK_PROVENANCE.satisfaction} />}
        >
          {t('dashboard.satisfaction.title')}
        </Header>
      }
    >
      <SpaceBetween size="l">
        <ColumnLayout columns={3} variant="text-grid">
          <SpaceBetween size="xxs">
            <Box variant="awsui-key-label">{t('dashboard.satisfaction.csat')}</Box>
            <Box fontSize="display-l" fontWeight="bold">
              {s.csat.toFixed(1)}
              <Box variant="span" color="text-body-secondary" fontSize="heading-m">
                {` / ${s.csatScale}`}
              </Box>
            </Box>
          </SpaceBetween>
          <SpaceBetween size="xxs">
            <Box variant="awsui-key-label">{t('dashboard.satisfaction.thumbs')}</Box>
            <Box fontSize="display-l" fontWeight="bold">{`${upPct.toFixed(0)}%`}</Box>
            <Box variant="small" color="text-body-secondary">
              {`${s.thumbsUp} / ${s.thumbsDown}`}
            </Box>
          </SpaceBetween>
          <SpaceBetween size="xxs">
            <Box variant="awsui-key-label">{t('dashboard.satisfaction.latestEscalation')}</Box>
            <Box fontSize="display-l" fontWeight="bold">
              {`${(s.escalationTrend[s.escalationTrend.length - 1].rate * 100).toFixed(1)}%`}
            </Box>
          </SpaceBetween>
        </ColumnLayout>

        <ChartTableToggle
          chart={escalationChart}
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
    </Container>
  );
}
