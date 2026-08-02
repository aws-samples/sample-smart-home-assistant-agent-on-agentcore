/**
 * Number formatting shared across the ops wall.
 *
 * Large standalone values use the font's PROPORTIONAL figures (no
 * tabular-nums): equal-width digits make a number like `121` look loose at
 * display sizes. Reserve tabular alignment for table columns and axis ticks.
 */

/** 1284 -> 1.3K, 683912 -> 684K. */
export function compact(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '--';
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(digits)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(digits)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(digits)}K`;
  if (abs >= 10) return n.toFixed(0);
  return n.toFixed(digits);
}

/** Sub-second stays in ms; past that switch to seconds so the tile stays short. */
export function ms(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '--';
  return n >= 1000 ? `${(n / 1000).toFixed(2)}s` : `${n.toFixed(0)}ms`;
}

/** 0.3053 -> "30.5%"; null renders as -- rather than a misleading 0%. */
export function pct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '--';
  return `${(n * 100).toFixed(digits)}%`;
}

/** Trim an ISO-ish timestamp to `YYYY-MM-DD HH:MM:SS`. */
export function stamp(s: string | undefined | null): string {
  return s ? s.slice(0, 19).replace('T', ' ') : '--';
}

/** `2026-07-21 00:00:00.000` / ISO -> `07-21` for dense axis ticks. */
export function dayTick(raw: string): string {
  return raw ? raw.slice(5, 10) : '';
}
