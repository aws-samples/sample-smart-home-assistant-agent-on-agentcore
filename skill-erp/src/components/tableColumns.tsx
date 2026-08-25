import React, { useCallback, useState } from 'react';
import type { TableProps } from '@cloudscape-design/components/table';

/**
 * How table columns behave: a width scale, a wrapping cell, and user-resizable widths.
 *
 * All three belong together because they only make sense as a set. Every table in this app
 * is `resizableColumns`, which changes what a width means, which changes how a prose cell
 * has to wrap. Splitting them up is how they drift apart.
 *
 * A near-identical copy lives in admin-console/src/components/tableColumns.tsx. The two
 * frontends share no build, and each keeps its own storage key so one app's saved
 * layout cannot disturb the other's.
 *
 * ## What resizableColumns actually does, measured
 *
 * Two behaviours are not what the API docs suggest, and both were found by deploying and
 * measuring rather than by reading:
 *
 *   - **A column with no `width` collapses to 120px.** The docs say an unset width means
 *     "the browser automatically adjusts the column width based on the content"; that is
 *     true only when `resizableColumns` is off. With it on, every unset column rendered at
 *     exactly 120px — the resize minimum — so skill names came out as "smart-scene-..."
 *     and dates truncated. Hence the scale below: every column needs a starting width.
 *   - **`maxWidth` is ignored entirely** (this one the docs do say). It is also the wrong
 *     shape for a resizable column: a cap enforced from inside the cell would silently
 *     refuse to use the width the user just dragged to.
 *
 * A third, from the docs and worth remembering while reading the tables: the last visible
 * column always absorbs the remaining space and its `width` is ignored, so there is no
 * point setting one on it.
 *
 * ## The width scale
 *
 * Calibrated from what the tables measured BEFORE they were made resizable — i.e. from the
 * widths the content-based layout had chosen and which looked right in production — then
 * rounded up so nothing that fits today starts out truncated. Recorded here because the
 * alternative is ~150 loose numbers spread across 31 tables, each of which encodes one
 * measurement of one day's data (the longest email in the fleet, the longest skill name).
 *
 * Sampled: email 258, status 176-245, date+time 192, uuid 262-341, name 173-224,
 * version/count 93-111, badge list 133-301, action buttons 100-280.
 */
export const W_NUM = 110;      // a version, a count, a latency
export const W_BADGE = 150;    // one or two badges, a short enum
export const W_STATUS = 210;   // status text, up to PENDING_APPROVAL with its icon
//                             ^ 180 first, which CLIPPED PENDING_APPROVAL in 5 of 6
//                               rows: the 176 sample this was calibrated from was the
//                               text alone, and StatusIndicator adds an icon and a gap.
export const W_DATE = 200;     // a date with a time on one line
export const W_NAME = 230;     // a skill / agent / scenario name
export const W_EMAIL = 260;    // an email address
export const W_WIDE = 300;     // a uuid, an endpoint, a list of tool badges
export const W_XWIDE = 340;    // a filename, or a cell holding its own Select

/** Prose: a description, a reason. An initial width the reader can drag, not a cap. */
export const TEXT_COL_MAX = 420;

/** Prose that IS the row rather than a label on it — a memory record, a scan finding. */
export const LONG_TEXT_COL_MAX = 600;

/**
 * A prose table cell: wraps to whatever the column currently is, instead of truncating.
 *
 * Carries no width of its own, deliberately. It used to, back when a column could not be
 * bounded any other way, and that is precisely what would fight the resizer now.
 *
 * Per cell rather than the Table's `wrapLines` prop, which applies to every column: on the
 * Registered Skills table that stacked the short structured columns letter by letter —
 * `PENDING_APPROVAL` rendered as "PE / NDING / _APPR / OVAL" and the View button as
 * "Vi / e / w". Those columns are meant to hold one line and truncate.
 *
 * `overflowWrap` is for the tokens prose CONTAINS but is not made of — a URL, a record id
 * — which have no space to break at and would otherwise overflow a narrowed column.
 */
export const WrapCell: React.FC<{ children?: React.ReactNode }> = ({ children }) => (
  <span style={{ whiteSpace: 'normal', overflowWrap: 'break-word' }}>{children}</span>
);

// ---------------------------------------------------------------------------
// User-resizable widths, remembered
// ---------------------------------------------------------------------------

const STORAGE_KEY = 'smarthome-skill-erp-column-widths';

/** `{ [tableId]: { [columnId]: width } }` */
type WidthStore = Record<string, Record<string, number>>;

function load(): WidthStore {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    // Anything that is not the shape we wrote is discarded rather than trusted: a
    // half-written or hand-edited value would otherwise throw on every render.
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as WidthStore)
      : {};
  } catch {
    return {};
  }
}

function save(store: WidthStore) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch {
    /* Private mode, or the quota is full. Resizing still works for this session. */
  }
}

/** What to spread onto a `<Table>`: the resize props plus width-merged definitions. */
export interface ResizableTableProps<T> {
  resizableColumns: true;
  columnDefinitions: ReadonlyArray<TableProps.ColumnDefinition<T>>;
  onColumnWidthsChange: (event: { detail: { widths: ReadonlyArray<number> } }) => void;
}

/**
 * Makes a table's columns resizable and remembers what the user chose.
 *
 * Without the remembering this reads as broken rather than as a feature: you drag a column,
 * navigate to another page, come back, and it is where it started. So the widths go to
 * localStorage, the same way the language and theme preferences already do.
 *
 * **Widths are stored per column id, not positionally.** Cloudscape reports a resize as
 * `{ widths: number[] }` aligned to the current column order. Persisting that array as-is
 * works until someone inserts a column, at which point every stored width shifts onto the
 * wrong column and the table comes back mangled in a way that reads as a rendering bug.
 * Zipping the array against the definitions on the way in means an added, removed or
 * reordered column simply has no stored width and falls back to its default.
 *
 * Usage — one hook call per component, then one wrapper call per table. The explicit type
 * argument is required: passing the array as a function argument means TypeScript can no
 * longer infer the row type from the Table's `items`.
 *
 *     const resizable = useResizableTables();
 *     ...
 *     <Table
 *       {...resizable<SkillItem>('skills', [
 *         { id: 'name', header: ..., width: W_NAME, cell: ... },
 *         { id: 'description', header: ..., width: TEXT_COL_MAX,
 *           cell: (s) => <WrapCell>{s.description}</WrapCell> },
 *       ])}
 *       items={skills}
 *     />
 *
 * The table id is a stable slug and is the storage key, so renaming one silently discards
 * that table's saved widths.
 */
export function useResizableTables() {
  const [store, setStore] = useState<WidthStore>(load);

  return useCallback(
    function resizable<T>(
      tableId: string,
      columnDefinitions: ReadonlyArray<TableProps.ColumnDefinition<T>>,
    ): ResizableTableProps<T> {
      const saved = store[tableId] || {};
      return {
        resizableColumns: true,
        // A saved width wins over the column's default, which is the whole point: the
        // default is a starting guess, the saved value is what this reader decided.
        columnDefinitions: columnDefinitions.map((col) => {
          const width = col.id ? saved[col.id] : undefined;
          return width ? { ...col, width } : col;
        }),
        onColumnWidthsChange: ({ detail }) => {
          const next: Record<string, number> = {};
          columnDefinitions.forEach((col, i) => {
            const w = detail.widths[i];
            if (col.id && typeof w === 'number') next[col.id] = w;
          });
          setStore((prev) => {
            const merged = { ...prev, [tableId]: { ...(prev[tableId] || {}), ...next } };
            save(merged);
            return merged;
          });
        },
      };
    },
    [store],
  );
}
