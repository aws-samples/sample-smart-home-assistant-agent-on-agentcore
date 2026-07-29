/**
 * Chart palette for the Overview ops dashboard.
 *
 * These hexes are NOT hand-picked. Cloudscape's own 8-slot categorical palette
 * fails the colorblind-safety gates outright — measured with the dataviz
 * validator (OKLab ΔE ×100, Machado-Oliveira-Fernandes 2009 at severity 1.0):
 *
 *   light: #096f64 chroma 0.085 (below the 0.10 floor, reads gray);
 *          #096f64 <-> #962249 deuteranopia ΔE 4.6 (needs >= 8)
 *   dark:  five slots outside the L 0.48-0.67 band; #ffb0c8 chroma 0.097;
 *          #40bfa9 <-> #ffb0c8 protanopia ΔE 4.6
 *
 * So we snap-to-passing within Cloudscape's OWN ramp steps: all
 * (hue family x step) triples were enumerated and validated for BOTH modes
 * under `--pairs all` (a line chart lets any two series neighbour, so the
 * harder all-pairs test applies, not just adjacent), keeping the triple with
 * the largest worst-case separation.
 *
 * Result — all six checks PASS in both modes:
 *   light  worst all-pairs CVD ΔE 12.0 (deutan), normal-vision 20.6
 *   dark   worst all-pairs CVD ΔE 13.0 (deutan), normal-vision 19.0
 *
 * HARD CAP: three series. A fourth colour cannot clear the floors — fold the
 * tail into "Other" or facet into small multiples instead of adding one.
 */

export type ChartTheme = 'light' | 'dark';

/** Categorical slots 1..3. Colour follows the ENTITY, never its rank. */
const CATEGORICAL: Record<ChartTheme, readonly [string, string, string]> = {
  // blue-2-300, pink-400, yellow-300 from Cloudscape's chart ramps
  light: ['#688ae8', '#ce567c', '#b2911c'],
  dark: ['#486de8', '#d56889', '#977001'],
};

export const MAX_SERIES = 3;

export function seriesColor(theme: ChartTheme, slot: number): string {
  const slots = CATEGORICAL[theme];
  // Never cycle: a 9th (or 4th) series must be folded upstream, not recoloured.
  return slots[Math.min(slot, MAX_SERIES - 1)];
}

/**
 * Status colours are RESERVED for state (good -> critical) and are never used
 * as "series 4". They always ship with an icon + text label, so colour alone
 * never carries the meaning — on the light surface `warning` is sub-3:1 by
 * design and the label is the mitigation.
 */
export const STATUS: Record<ChartTheme, Record<'good' | 'warning' | 'serious' | 'critical', string>> = {
  light: { good: '#67a353', warning: '#b2911c', serious: '#cc5f21', critical: '#ba2e0f' },
  dark: { good: '#69ae34', warning: '#dfb52c', serious: '#f89256', critical: '#fe6e73' },
};

/** De-emphasis grey for the "context" series in an emphasis chart. */
export const DE_EMPHASIS: Record<ChartTheme, string> = {
  light: '#8c8c94',
  dark: '#8c8c94',
};

/**
 * Severity for a 0..1 utilisation ratio (meter fill). Returns a status key so
 * the caller can pair it with the matching icon + label.
 */
export function utilisationSeverity(ratio: number): 'good' | 'warning' | 'serious' | 'critical' {
  if (ratio >= 0.95) return 'critical';
  if (ratio >= 0.8) return 'serious';
  if (ratio >= 0.6) return 'warning';
  return 'good';
}

/** Error-rate severity. Same reserved scale; error rate MEANS good/bad. */
export function errorRateSeverity(rate: number): 'good' | 'warning' | 'serious' | 'critical' {
  if (rate >= 0.25) return 'critical';
  if (rate >= 0.1) return 'serious';
  if (rate >= 0.02) return 'warning';
  return 'good';
}
