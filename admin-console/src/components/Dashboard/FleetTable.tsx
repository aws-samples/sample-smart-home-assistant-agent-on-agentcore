import React from 'react';
import Box from '@cloudscape-design/components/box';
import Table from '@cloudscape-design/components/table';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import { RuntimeBreakdown } from '../../api/adminApi';
import { compact, ms } from './format';
import { useI18n } from '../../i18n';

interface Props {
  runtimes?: RuntimeBreakdown[];
  loading: boolean;
}

/**
 * Counts, formatted as counts.
 *
 * `compact()` keeps one decimal below 10 — right for the big KPI tiles it was
 * written for, wrong here: these are event counts, and "3 errors" rendered as
 * "3.0" reads like a measurement. Values in the thousands still get compacted,
 * since a column is not the place for `12345`.
 */
function count(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '--';
  return Math.abs(n) < 1000 ? Math.round(n).toString() : compact(n);
}

/**
 * Per-runtime split of the status strip's fleet-wide totals.
 *
 * The strip above answers "is anything wrong?" across every registered runtime
 * at once. Once there is more than one agent that aggregate hides the thing you
 * actually want next: WHICH agent. This table is that follow-up, so it sits
 * directly under the strip rather than in the panel grid.
 *
 * Collapsed by default — on a wall someone is watching, the aggregate is the
 * headline and this is the drill-down.
 *
 * A runtime showing all zeros is real, not a gap: a deployed agent that has not
 * been invoked in the selected window emits no metrics. Sorting is left as the
 * backend's (name-ascending) so the row order is stable between refreshes.
 */
export function FleetTable({ runtimes, loading }: Props) {
  const { t } = useI18n();
  const items = runtimes ?? [];

  // Older backends don't return the breakdown at all. Rendering an empty
  // "0 runtimes" section would read as "the fleet is gone", so say nothing.
  if (!loading && items.length === 0) return null;

  return (
    <ExpandableSection
      headerText={t('dashboard.fleet.title')}
      variant="footer"
      headerCounter={items.length ? `(${items.length})` : undefined}
    >
      <Box variant="small" color="text-body-secondary" padding={{ bottom: 'xs' }}>
        {t('dashboard.fleet.desc')}
      </Box>
      <Table
        variant="embedded"
        contentDensity="compact"
        loading={loading && items.length === 0}
        loadingText={t('dashboard.loading')}
        items={items}
        trackBy="name"
        columnDefinitions={[
          {
            id: 'name',
            header: t('dashboard.fleet.runtime'),
            cell: (r) => r.name,
          },
          {
            id: 'invocations',
            header: t('dashboard.fleet.invocations'),
            cell: (r) => count(r.invocations),
          },
          {
            id: 'sessions',
            header: t('dashboard.fleet.sessions'),
            cell: (r) => count(r.sessions),
          },
          {
            id: 'errors',
            header: t('dashboard.fleet.errors'),
            cell: (r) => count((r.userErrors ?? 0) + (r.systemErrors ?? 0)),
          },
          {
            id: 'throttles',
            header: t('dashboard.fleet.throttles'),
            cell: (r) => count(r.throttles),
          },
          {
            id: 'latency',
            header: t('dashboard.fleet.latencyP95'),
            // AgentCore's Latency metric times the WHOLE invocation, so a
            // multi-turn tool-using conversation lands in the tens of seconds.
            // That is not the TTFT on the status strip above — hence the
            // separate wording, so the two are not read as contradicting.
            // null (no samples) renders as -- rather than a misleading 0ms.
            cell: (r) => ms(r.latencyP95),
          },
        ]}
        empty={
          <Box textAlign="center" padding="m" color="text-body-secondary">
            {t('dashboard.noData')}
          </Box>
        }
      />
    </ExpandableSection>
  );
}
