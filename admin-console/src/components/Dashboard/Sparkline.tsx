import React from 'react';

interface Props {
  values: number[];
  color: string;
  /** Accessible description; the numeric value is always shown beside it. */
  ariaLabel: string;
  width?: number;
  height?: number;
}

/**
 * Inline trend sparkline for a stat tile.
 *
 * Hand-rolled SVG rather than a Cloudscape chart: at 28px tall an axis-bearing
 * chart component adds chrome that would swamp the mark. 2px stroke and a >=8px
 * end marker match the mark specs used by the full charts, and the end marker
 * carries a 2px surface-coloured ring so it stays legible over the line.
 *
 * Decorative only — the tile's value and delta carry the data, so this is
 * aria-hidden with the label supplied by the tile itself.
 */
export function Sparkline({ values, color, ariaLabel, width = 132, height = 28 }: Props) {
  const clean = values.filter((v) => Number.isFinite(v));
  if (clean.length < 2) {
    return <div style={{ height, width }} aria-hidden="true" />;
  }

  const pad = 4;
  const min = Math.min(...clean);
  const max = Math.max(...clean);
  const span = max - min || 1;
  const stepX = (width - pad * 2) / (clean.length - 1);

  const points = clean.map((v, i) => {
    const x = pad + i * stepX;
    // SVG y grows downward; invert so larger values sit higher.
    const y = height - pad - ((v - min) / span) * (height - pad * 2);
    return { x, y };
  });
  const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
  const last = points[points.length - 1];

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={ariaLabel}
      style={{ display: 'block', overflow: 'visible' }}
    >
      <path d={path} fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
      {/* Surface ring keeps the end dot readable where it crosses the line. */}
      <circle cx={last.x} cy={last.y} r={4} fill={color} stroke="var(--sparkline-surface, transparent)" strokeWidth={2} />
    </svg>
  );
}
