/**
 * Light effect renderer: (colors, effect, speed, tick) -> per-segment colors.
 *
 * This replaced the eight hardcoded `case` blocks in the old LedMatrix, each of
 * which computed one named look from a closed-form function of (tick, row, col).
 * That shape only renders effects someone wrote a branch for, which caps an
 * effect-generating agent at picking from a menu of eight.
 *
 * Here an effect is a *motion* applied to an arbitrary color list, so any
 * palette the agent invents renders — "ocean" is a blue-green list on a `wave`,
 * "fire" is an orange list on `flicker`. Segment count is a parameter, so the
 * same effect adapts to a 30-segment strip or a 256-pixel matrix.
 */

export type EffectName =
  | 'solid'
  | 'gradient'
  | 'wave'
  | 'chase'
  | 'breathe'
  | 'sparkle'
  | 'twinkle'
  | 'flicker';

export interface EffectSpec {
  effect: EffectName;
  /** Palette in #RRGGBB. Empty falls back to white so a bad spec still lights up. */
  colors: string[];
  /** 1 (slowest) to 10 (fastest). */
  speed: number;
  /** 0-100, applied last as a lightness multiplier. */
  brightness: number;
}

interface Hsl {
  h: number;
  s: number;
  l: number;
}

const WHITE: Hsl = { h: 0, s: 0, l: 100 };

export function hexToHsl(hex: string): Hsl {
  const clean = hex.replace('#', '');
  const r = parseInt(clean.slice(0, 2), 16) / 255;
  const g = parseInt(clean.slice(2, 4), 16) / 255;
  const b = parseInt(clean.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  const l = (max + min) / 2;
  let h = 0;
  let s = 0;
  if (d !== 0) {
    s = d / (1 - Math.abs(2 * l - 1));
    if (max === r) h = 60 * (((g - b) / d) % 6);
    else if (max === g) h = 60 * ((b - r) / d + 2);
    else h = 60 * ((r - g) / d + 4);
  }
  return { h: (h + 360) % 360, s: s * 100, l: l * 100 };
}

export function hslToHex({ h, s, l }: Hsl): string {
  const hue = ((h % 360) + 360) % 360;
  const sat = Math.max(0, Math.min(100, s)) / 100;
  const lig = Math.max(0, Math.min(100, l)) / 100;
  const c = (1 - Math.abs(2 * lig - 1)) * sat;
  const x = c * (1 - Math.abs(((hue / 60) % 2) - 1));
  const m = lig - c / 2;
  let r = 0;
  let g = 0;
  let b = 0;
  if (hue < 60) { r = c; g = x; } else if (hue < 120) { r = x; g = c; } else if (hue < 180) { g = c; b = x; } else if (hue < 240) { g = x; b = c; } else if (hue < 300) { r = x; b = c; } else { r = c; b = x; }
  const hx = (v: number) => Math.round((v + m) * 255).toString(16).padStart(2, '0');
  return `#${hx(r)}${hx(g)}${hx(b)}`;
}

/**
 * Sample a palette at `t` in [0,1), interpolating around a ring.
 *
 * Hue interpolation takes the SHORT way around the circle, so red -> magenta
 * stays in the reds instead of sweeping through green. Wrapping back to
 * colors[0] at t=1 is what keeps looping effects seamless.
 */
export function sampleRing(colors: Hsl[], t: number): Hsl {
  if (colors.length === 0) return WHITE;
  if (colors.length === 1) return colors[0];
  const wrapped = ((t % 1) + 1) % 1;
  const scaled = wrapped * colors.length;
  const i = Math.floor(scaled) % colors.length;
  const next = (i + 1) % colors.length;
  const f = scaled - Math.floor(scaled);
  const a = colors[i];
  const b = colors[next];
  let dh = b.h - a.h;
  if (dh > 180) dh -= 360;
  if (dh < -180) dh += 360;
  return {
    h: a.h + dh * f,
    s: a.s + (b.s - a.s) * f,
    l: a.l + (b.l - a.l) * f,
  };
}

/** Deterministic per-(segment, bucket) pseudo-random, so sparkle doesn't restrobe every frame. */
function hashNoise(seed: number): number {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

/**
 * Render one frame.
 *
 * @param count  number of segments/pixels to fill
 * @param tick   monotonic frame counter
 */
export function renderFrame(spec: EffectSpec, count: number, tick: number): string[] {
  const palette = (spec.colors.length ? spec.colors : ['#ffffff']).map(hexToHsl);
  const bMul = Math.max(0, Math.min(100, spec.brightness)) / 100;
  // Normalise speed so 1..10 spans a useful range rather than crawling at both ends.
  const rate = (Math.max(1, Math.min(10, spec.speed)) / 10) * 0.08;
  const phase = tick * rate;
  const out: string[] = new Array(count);

  for (let i = 0; i < count; i++) {
    const pos = count > 1 ? i / count : 0;
    let hsl: Hsl;

    switch (spec.effect) {
      case 'solid':
        hsl = palette[0];
        break;

      case 'gradient':
        // Static spread of the palette across the strip.
        hsl = sampleRing(palette, pos);
        break;

      case 'wave': {
        // Palette drifts along the strip while lightness undulates — the shape
        // the old 'ocean' branch produced, now palette-driven.
        const base = sampleRing(palette, pos + phase * 0.5);
        const swell = Math.sin(pos * Math.PI * 2 - phase * 2) * 0.5 + 0.5;
        hsl = { ...base, l: base.l * (0.45 + swell * 0.55) };
        break;
      }

      case 'chase': {
        // A bright head with a fading tail, cycling around the strip.
        const head = (phase * 1.5) % 1;
        let dist = pos - head;
        if (dist < 0) dist += 1;
        const tail = Math.max(0, 1 - dist * 6);
        const base = sampleRing(palette, pos);
        hsl = { ...base, l: base.l * (0.06 + tail * 0.94) };
        break;
      }

      case 'breathe': {
        // Whole strip pulses together.
        const breath = (Math.sin(phase * 2) + 1) / 2;
        const base = sampleRing(palette, pos * 0.25 + phase * 0.1);
        hsl = { ...base, l: base.l * (0.12 + breath * 0.88) };
        break;
      }

      case 'sparkle': {
        // Bucket the tick so a lit segment stays lit for a few frames instead of
        // strobing at the frame rate.
        const bucket = Math.floor(phase * 6);
        const lit = hashNoise(i * 7.13 + bucket) > 0.86;
        const base = sampleRing(palette, pos);
        hsl = lit
          ? { ...base, l: Math.min(100, base.l * 1.25) }
          : { ...base, l: base.l * 0.1 };
        break;
      }

      case 'twinkle': {
        // Every segment breathes on its own offset — softer than sparkle.
        const own = Math.sin(phase * 3 + hashNoise(i * 3.77) * Math.PI * 2) * 0.5 + 0.5;
        const base = sampleRing(palette, pos);
        hsl = { ...base, l: base.l * (0.2 + own * 0.8) };
        break;
      }

      case 'flicker': {
        // Warm, unsteady, biased bright at one end — the old 'fire' look.
        const heat = Math.max(
          0,
          1 - pos + Math.sin(phase * 4 + i * 0.8) * 0.3 + (hashNoise(i + Math.floor(phase * 8)) - 0.5) * 0.35,
        );
        const base = sampleRing(palette, Math.min(1, heat));
        hsl = { ...base, l: base.l * Math.min(1, heat) };
        break;
      }

      default:
        hsl = sampleRing(palette, pos);
    }

    out[i] = hslToHex({ ...hsl, l: hsl.l * bMul });
  }

  return out;
}

/**
 * Named palettes for the effects the LED matrix exposed as modes, so its
 * existing buttons and any skill that sends `setMode` keep working.
 */
export const MODE_PRESETS: Record<string, { effect: EffectName; colors: string[]; speed: number }> = {
  rainbow: {
    effect: 'gradient',
    colors: ['#ff0000', '#ffff00', '#00ff00', '#00ffff', '#0000ff', '#ff00ff'],
    speed: 5,
  },
  breathing: { effect: 'breathe', colors: ['#ff2d95', '#7c3aed'], speed: 3 },
  chase: { effect: 'chase', colors: ['#22d3ee', '#a78bfa', '#f472b6'], speed: 6 },
  sparkle: { effect: 'sparkle', colors: ['#ffffff', '#fde047', '#93c5fd'], speed: 5 },
  fire: { effect: 'flicker', colors: ['#450a0a', '#dc2626', '#f59e0b', '#fef08a'], speed: 7 },
  ocean: { effect: 'wave', colors: ['#0c4a6e', '#0891b2', '#22d3ee', '#a5f3fc'], speed: 4 },
  aurora: { effect: 'wave', colors: ['#065f46', '#10b981', '#60a5fa', '#a78bfa'], speed: 2 },
  solid: { effect: 'solid', colors: ['#ff0000'], speed: 1 },
};

/** Kelvin -> approximate #RRGGBB, for color_temp on white-tunable lights. */
export function kelvinToHex(kelvin: number): string {
  const k = Math.max(1000, Math.min(12000, kelvin)) / 100;
  const ch = (v: number) => Math.round(Math.max(0, Math.min(255, v)));
  let r: number;
  let g: number;
  let b: number;
  if (k <= 66) {
    r = 255;
    g = 99.47 * Math.log(k) - 161.12;
    b = k <= 19 ? 0 : 138.52 * Math.log(k - 10) - 305.04;
  } else {
    r = 329.7 * Math.pow(k - 60, -0.1332);
    g = 288.12 * Math.pow(k - 60, -0.0755);
    b = 255;
  }
  return `#${ch(r).toString(16).padStart(2, '0')}${ch(g).toString(16).padStart(2, '0')}${ch(b).toString(16).padStart(2, '0')}`;
}
