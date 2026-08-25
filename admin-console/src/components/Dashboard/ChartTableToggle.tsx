import React, { useState } from 'react';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import Table from '@cloudscape-design/components/table';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { useI18n } from '../../i18n';
import { useResizableTables } from '../tableColumns';

export interface TableColumn<T> {
  id: string;
  header: string;
  cell: (item: T) => React.ReactNode;
  /** Starting width, from the scale in ../tableColumns. Needed because these tables are
   *  resizable, and a resizable column with no width collapses to 120px. */
  width?: number;
}

interface Props<T> {
  /** The chart to show in chart mode. */
  chart: React.ReactNode;
  /** Row data backing the table view. */
  items: T[];
  columns: TableColumn<T>[];
  /** Caveat shown under BOTH views — e.g. where span history actually starts. */
  footer?: React.ReactNode;
  /** Stable slug for remembering this table's column widths. Required rather than
   *  optional: one component renders several different tables, and sharing a storage
   *  key between them would have each chart's twin overwrite the others' widths. */
  tableId: string;
}

/**
 * Chart / table view switch.
 *
 * Every chart ships a table twin: a tooltip must never be the ONLY way to read
 * a value (three of the light-mode chart hues sit below 3:1 against the white
 * surface, so the table is also the documented contrast relief channel).
 */
export function ChartTableToggle<T>({ chart, items, columns, footer, tableId }: Props<T>) {
  const { t } = useI18n();
  const resizable = useResizableTables();
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
          {...resizable<T>(tableId, columns)}
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
