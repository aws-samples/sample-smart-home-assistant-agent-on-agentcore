import React, { useCallback, useEffect, useState } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Grid from '@cloudscape-design/components/grid';
import Select from '@cloudscape-design/components/select';
import Button from '@cloudscape-design/components/button';
import Alert from '@cloudscape-design/components/alert';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import {
  DashboardDim,
  DashboardFastResponse,
  DashboardRange,
  DashboardSpansResponse,
  getDashboardFast,
  getDashboardSpans,
} from '../../api/adminApi';
import { ChartTheme } from './palette';
import { StatusStrip } from './StatusStrip';
import { Panel } from './Panel';
import { TokenTrend, TokenAttribution } from './TokenCharts';
import { BudgetMeter } from './BudgetMeter';
import { EvalPanel } from './EvalPanel';
import { ReleaseTable } from './ReleaseTable';
import { SatisfactionCards } from './SatisfactionCards';
import { MOCK_PROVENANCE } from './mockData';
import { stamp } from './format';
import { useI18n } from '../../i18n';

interface Props {
  theme: ChartTheme;
}

const RANGES: DashboardRange[] = ['24h', '7d', '30d'];
const DIMS: DashboardDim[] = ['user', 'tenant', 'agent'];

/**
 * Every plot on the wall shares one height so the grid rows line up. Without
 * this, panels in the same row end at different baselines and the wall reads
 * ragged. Includes room for the x-axis band — a fixed height that excludes the
 * axis labels gives the card a nested scrollbar.
 */
const PLOT_HEIGHT = 180;

/**
 * Agent ops dashboard, laid out as a monitoring wall.
 *
 * The layout is the point here. Six full-width cards stacked vertically meant
 * ~5000px of scrolling and no way to correlate two signals, so this is
 * restructured into the standard NOC shape:
 *
 *   row 0   filter bar (scopes everything below it)
 *   row 1   status strip — 6 live signals, the "is anything wrong?" band
 *   row 2   token trend (8 cols) | budget (4 cols)
 *   row 3   evaluation quality (6 cols) | release state (6 cols)
 *   row 4   token attribution (6 cols) | satisfaction (6 cols)
 *
 * Wide-and-shallow beats tall-and-narrow for time series, hence the 8/4 split
 * on the trend row. Below the `s` breakpoint every panel goes full width, since
 * two columns of dense charts on a narrow screen is worse than one.
 *
 * Columns switch at `s` (912px), NOT `m` (1120px): Cloudscape measures
 * breakpoints against the CONTAINER, and the console's nav rail leaves ~1078px
 * at a 1680px viewport — so an `m` split silently never engaged on a normal
 * desktop and every panel rendered full width.
 *
 * Loading stays two-stage: fast metric reads paint immediately, the ~5-20s Logs
 * Insights query fills the token panels in when it lands. On refetch the prior
 * render is HELD at reduced opacity rather than replaced by skeletons — no
 * layout jump on a wall someone is watching.
 */
export function DashboardSection({ theme }: Props) {
  const { t } = useI18n();
  const [range, setRange] = useState<DashboardRange>('7d');
  const [dim, setDim] = useState<DashboardDim>('user');

  const [fast, setFast] = useState<DashboardFastResponse | undefined>();
  const [spans, setSpans] = useState<DashboardSpansResponse | undefined>();
  const [fastLoading, setFastLoading] = useState(false);
  const [spansLoading, setSpansLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async (r: DashboardRange, d: DashboardDim, refresh: boolean) => {
    setError('');
    setFastLoading(true);
    setSpansLoading(true);
    const fastPromise = getDashboardFast(r, refresh)
      .then(setFast)
      .catch((e) => setError(e.message))
      .finally(() => setFastLoading(false));
    // A spans failure must never blank the fast panels.
    const spansPromise = getDashboardSpans(r, d, refresh)
      .then(setSpans)
      .catch(() => setSpans(undefined))
      .finally(() => setSpansLoading(false));
    await Promise.allSettled([fastPromise, spansPromise]);
  }, []);

  useEffect(() => {
    void load(range, dim, false);
  }, [range, dim, load]);

  const loading = fastLoading || spansLoading;

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={t('dashboard.desc')}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {fast?.cached && fast.cachedAt && (
                <Box variant="small" color="text-body-secondary" padding={{ top: 'xxs' }}>
                  {t('dashboard.cachedAt').replace('{time}', stamp(fast.cachedAt))}
                </Box>
              )}
              <Button iconName="refresh" loading={loading} onClick={() => void load(range, dim, true)}>
                {t('dashboard.refresh')}
              </Button>
            </SpaceBetween>
          }
        >
          {t('dashboard.title')}
        </Header>
      }
    >
      <SpaceBetween size="l">
        {/* Time range scopes every panel, so it stays here. The attribution
            dimension only affects the Token attribution panel and now lives in
            that panel's header — a global-looking control that changes one card
            reads as a bug. Range is a segmented control because three
            mutually-exclusive presets are a switch, not a dropdown to open. */}
        <SegmentedControl
          selectedId={range}
          onChange={({ detail }) => setRange(detail.selectedId as DashboardRange)}
          label={t('dashboard.filter.range')}
          options={RANGES.map((r) => ({ id: r, text: t(`dashboard.range.${r}`) }))}
        />

        {error && (
          <Alert type="error" dismissible onDismiss={() => setError('')}>
            {error}
          </Alert>
        )}

        {fast?.health && !fast.health.available && (
          <Alert type="warning">
            {t('dashboard.health.unavailable').replace('{reason}', fast.health.reason || '')}
          </Alert>
        )}

        <div style={{ opacity: loading && fast ? 0.6 : 1, transition: 'opacity 120ms ease' }}>
          <SpaceBetween size="l">
            <Container>
              <StatusStrip
                fast={fast}
                spans={spans?.spans}
                spansLoading={spansLoading}
                theme={theme}
              />
            </Container>

            {/* Time series gets the wide cell; the meter stack is naturally narrow. */}
            <Grid
              gridDefinition={[
                { colspan: { default: 12, s: 8 } },
                { colspan: { default: 12, s: 4 } },
              ]}
            >
              <Panel title={t('dashboard.token.trendTitle')} info={t('dashboard.token.trendDesc')}>
                <TokenTrend
                  spans={spans?.spans}
                  loading={spansLoading}
                  theme={theme}
                  chartHeight={PLOT_HEIGHT}
                />
              </Panel>
              <Panel
                title={t('dashboard.budget.title')}
                info={t('dashboard.budget.desc')}
                demoProvenanceKey={MOCK_PROVENANCE.budget}
              >
                <BudgetMeter />
              </Panel>
            </Grid>

            {/* Quality beside release state: "did it get worse?" and "what
                changed?" are the two halves of the same investigation. */}
            <Grid
              gridDefinition={[
                { colspan: { default: 12, s: 6 } },
                { colspan: { default: 12, s: 6 } },
              ]}
            >
              <Panel title={t('dashboard.eval.title')} info={t('dashboard.eval.desc')}>
                <EvalPanel
                  evaluations={fast?.evaluations}
                  abComparison={fast?.abComparison}
                  loading={fastLoading}
                  theme={theme}
                  chartHeight={PLOT_HEIGHT}
                />
              </Panel>
              <Panel title={t('dashboard.release.title')} info={t('dashboard.release.desc')}>
                <ReleaseTable release={fast?.release} loading={fastLoading} />
              </Panel>
            </Grid>

            <Grid
              gridDefinition={[
                { colspan: { default: 12, s: 6 } },
                { colspan: { default: 12, s: 6 } },
              ]}
            >
              <Panel
                title={t('dashboard.token.attributionTitle')}
                info={t(`dashboard.token.attributionDesc.${dim}`)}
                actions={
                  // Scoped to this panel because it only affects this panel.
                  <div style={{ minWidth: 170 }}>
                    <Select
                      selectedOption={{ value: dim, label: t(`dashboard.dim.${dim}`) }}
                      onChange={({ detail }) => setDim(detail.selectedOption.value as DashboardDim)}
                      options={DIMS.map((d) => ({ value: d, label: t(`dashboard.dim.${d}`) }))}
                      ariaLabel={t('dashboard.filter.dim')}
                    />
                  </div>
                }
              >
                <TokenAttribution
                  spans={spans?.spans}
                  loading={spansLoading}
                  dim={dim}
                  theme={theme}
                  chartHeight={PLOT_HEIGHT}
                />
              </Panel>
              <Panel
                title={t('dashboard.satisfaction.title')}
                info={t('dashboard.satisfaction.desc')}
                demoProvenanceKey={MOCK_PROVENANCE.satisfaction}
              >
                <SatisfactionCards theme={theme} chartHeight={PLOT_HEIGHT} />
              </Panel>
            </Grid>
          </SpaceBetween>
        </div>
      </SpaceBetween>
    </Container>
  );
}
