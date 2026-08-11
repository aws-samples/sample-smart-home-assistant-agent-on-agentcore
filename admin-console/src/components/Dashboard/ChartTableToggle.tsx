import React, { useState } from 'react';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import Table from '@cloudscape-design/components/table';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { useI18n } from '../../i18n';

export interface TableColumn<T> {
  id: string;
  header: string;
  cell: (item: T) => React.ReactNode;
}

interface Props<T> {
  /** The chart to show in chart mode. */
  chart: React.ReactNode;
  /** Row data backing the table view. */
  items: T[];
  columns: TableColumn<T>[];
  /** Caveat shown under BOTH views — e.g. where span history actually starts. */
  footer?: React.ReactNode;
}

/**
 * Chart / table view switch.
 *
 * Every chart ships a table twin: a tooltip must never be the ONLY way to read
 * a value (three of the light-mode chart hues sit below 3:1 against the white
 * surface, so the table is also the documented contrast relief channel).
 */
export function ChartTableToggle<T>({ chart, items, columns, footer }: Props<T>) {
  const { t } = useI18n();
  const [view, setView] = useState<'chart' | 'table'>('chart');

  return (
    <SpaceBetween size="s">
      <Box float="right">
        <SegmentedControl
          selectedId={view}
          onChange={({ detail }) => setView(detail.selectedId as 'chart' | 'table')}
          label={t('dashboard.viewSwitchLabel')}
          options={[
            { id: 'chart', text: t('dashboard.viewChart'), iconName: 'view-full' },
            { id: 'table', text: t('dashboard.viewTable'), iconName: 'list-view' },
          ]}
        />
      </Box>
      {view === 'chart' ? (
        chart
      ) : (
        <Table
          variant="embedded"
          items={items}
          columnDefinitions={columns}
          empty={
            <Box textAlign="center" padding="m" color="text-body-secondary">
              {t('dashboard.noData')}
            </Box>
          }
        />
      )}
      {footer}
    </SpaceBetween>
  );
}
