import React from 'react';
import Box from '@cloudscape-design/components/box';
import LineChart from '@cloudscape-design/components/line-chart';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { DashboardSatisfaction } from '../../api/adminApi';
import { ChartTheme, seriesColor } from './palette';
import { ChartTableToggle } from './ChartTableToggle';
import { useI18n } from '../../i18n';

interface Props {
  data?: DashboardSatisfaction;
  theme: ChartTheme;
  chartHeight: number;
}

/**
 * #6 User satisfaction — REAL as of 2026-08-11.
 *
 * Reads the `smarthome-feedback` table, written by the chatbot's per-turn 👍/👎.
 * It was mock data until the chatbot had a feedback control at all: the only
 * feedback path was the `user-feedback` skill writing JSON into a runtime's
 * /mnt/workspace/feedback/, readable one file at a time through Remote Shell and
 * never aggregatable into a figure.
 *
 * The empty state is the part worth being careful about. With no votes this
 * renders "no feedback yet" and NOTHING else — no zero CSAT, no flat line at the
 * bottom of the chart. An empty table and universal dissatisfaction produce the
 * same pixels on a gauge and mean opposite things, and inventing the pessimistic
 * reading of missing data is the same mistake as inventing the optimistic one.
 *
 * `byAgent` counts a multi-specialist turn once per specialist. The question is
 * "does this agent correlate with dissatisfaction", not "who is at fault".
 */
export function SatisfactionCards({ data, theme, chartHeight }: Props) {
  const { t } = useI18n();

  if (!data?.available) {
    return (
      <Box color="text-body-secondary" textAlign="center" padding={{ vertical: 'l' }}>
        {t('dashboard.satisfaction.empty')}
      </Box>
    );
  }

  const up = data.thumbsUp ?? 0;
  const down = data.thumbsDown ?? 0;
  const total = up + down;
  const upPct = total ? (up / total) * 100 : 0;
  const trend = data.trend ?? [];
  const byAgent = data.byAgent ?? [];
  const reasons = data.recentReasons ?? [];
  const simShare = data.simulatedShare ?? 0;

  return (
    <SpaceBetween size="s">
      <SpaceBetween direction="horizontal" size="l">
        <div>
          <Box variant="awsui-key-label">{t('dashboard.satisfaction.csat')}</Box>
          <Box fontSize="heading-xl" fontWeight="bold">
            {data.csat != null ? data.csat.toFixed(1) : '-'}
            <Box variant="span" color="text-body-secondary" fontSize="body-m" fontWeight="normal">
              {` / ${data.csatScale ?? 5}`}
            </Box>
          </Box>
        </div>
        <div>
          <Box variant="awsui-key-label">{t('dashboard.satisfaction.thumbs')}</Box>
          <Box fontSize="heading-xl" fontWeight="bold">{`${upPct.toFixed(0)}%`}</Box>
          <Box variant="small" color="text-body-secondary">{`${up} / ${down}`}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">{t('dashboard.satisfaction.votes')}</Box>
          <Box fontSize="heading-xl" fontWeight="bold">{total}</Box>
        </div>
      </SpaceBetween>

      {/* Stated whenever any simulated vote is included. The simulator files
          votes through the same API as a real user, which is what makes the demo
          usable — and exactly why the share has to be visible. */}
      {simShare > 0 && (
        <Box variant="small" color="text-status-info">
          {t('dashboard.satisfaction.simulated').replace(
            '{pct}', (simShare * 100).toFixed(0))}
        </Box>
      )}

      <ChartTableToggle
        chart={
          <LineChart
            height={chartHeight}
            hideFilter
            hideLegend
            series={[
              {
                title: t('dashboard.satisfaction.downRate'),
                type: 'line',
                color: seriesColor(theme, 0),
                data: trend.map((p) => ({ x: p.day.slice(5), y: p.downRate * 100 })),
                valueFormatter: (v: number) => `${v.toFixed(1)}%`,
              },
            ]}
            xScaleType="categorical"
            yTitle="%"
            ariaLabel={t('dashboard.satisfaction.downRate')}
            yTickFormatter={(v: number) => `${v.toFixed(0)}%`}
          />
        }
        items={trend}
        columns={[
          { id: 'day', header: t('dashboard.token.xDay'), cell: (p) => p.day },
          { id: 'up', header: '👍', cell: (p) => p.up },
          { id: 'down', header: '👎', cell: (p) => p.down },
          {
            id: 'rate',
            header: t('dashboard.satisfaction.downRate'),
            cell: (p) => `${(p.downRate * 100).toFixed(1)}%`,
          },
        ]}
      />

      {/* Which specialist drew the vote — the question mock data could not
          answer, and the reason the vote carries the turn's delegation trace. */}
      {byAgent.length > 0 && (
        <div>
          <Box variant="awsui-key-label">{t('dashboard.satisfaction.byAgent')}</Box>
          <SpaceBetween size="xxs">
            {byAgent.slice(0, 6).map((r) => (
              <Box key={r.agent} variant="small">
                {`${r.agent}: 👍 ${r.up} / 👎 ${r.down}`}
              </Box>
            ))}
          </SpaceBetween>
        </div>
      )}

      {reasons.length > 0 && (
        <div>
          <Box variant="awsui-key-label">{t('dashboard.satisfaction.reasons')}</Box>
          <SpaceBetween size="xxs">
            {reasons.slice(0, 5).map((r) => (
              <Box key={`${r.ts}-${r.reason}`} variant="small" color="text-body-secondary">
                {`${r.ts.slice(0, 16).replace('T', ' ')} — ${r.reason}`}
              </Box>
            ))}
          </SpaceBetween>
        </div>
      )}
    </SpaceBetween>
  );
}
