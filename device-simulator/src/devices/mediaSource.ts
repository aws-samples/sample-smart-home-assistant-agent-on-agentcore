/**
 * The props a scene needs: a screen to sync a backlight to, and music to sync it
 * to. Both are generated — no assets, no audio files, no external streams.
 *
 * Why generated rather than played:
 *
 *  - A real video file would need hosting, a CDN path, and a licence, and the
 *    thing being demonstrated is the *sync*, not the content. A procedural frame
 *    has the property that matters — colour that changes over time in a way you
 *    can watch the backlight follow.
 *  - Real audio would need autoplay permission, which browsers refuse without a
 *    gesture, so a scene that starts the music feast would silently do nothing.
 *    A synthesised beat is a number that rises and falls on a tempo; the UI shows
 *    it and the lights pulse to it, which is what the scene claims to do.
 *
 * `edgeColors` is the piece that connects to a device: it samples the four screen
 * edges and returns one colour per edge, in the order the TV backlight's four
 * segments are laid out. That is the whole of "ambient screen sync" — the rest is
 * the effect engine the strip already has.
 */

export type SceneName = 'sunset' | 'ocean' | 'forest' | 'neon' | 'fireplace';

export interface MediaFrame {
  /** Four edge colours: top, right, bottom, left. */
  edges: [string, string, string, string];
  /** Mean luminance 0..1, for a brightness follow. */
  luminance: number;
}

/** Palettes to render. Named after what a user would ask a scene agent for. */
const SCENES: Record<SceneName, string[]> = {
  sunset: ['#ff6b35', '#f7931e', '#ffd23f', '#c1436d', '#5c2a9d'],
  ocean: ['#012a4a', '#013a63', '#01497c', '#2a6f97', '#61a5c2'],
  forest: ['#1b4332', '#2d6a4f', '#40916c', '#74c69d', '#b7e4c7'],
  neon: ['#ff006e', '#8338ec', '#3a86ff', '#06ffa5', '#ffbe0b'],
  fireplace: ['#3d0000', '#8b0000', '#ff4500', '#ff8c00', '#ffd700'],
};

export const SCENE_NAMES = Object.keys(SCENES) as SceneName[];

function hexToRgb(hex: string): [number, number, number] {
  const c = hex.replace('#', '');
  return [
    parseInt(c.slice(0, 2), 16),
    parseInt(c.slice(2, 4), 16),
    parseInt(c.slice(4, 6), 16),
  ];
}

function rgbToHex(r: number, g: number, b: number): string {
  const clamp = (v: number) => Math.max(0, Math.min(255, Math.round(v)));
  return '#' + [r, g, b].map((v) => clamp(v).toString(16).padStart(2, '0')).join('');
}

function mix(a: string, b: string, t: number): string {
  const [ar, ag, ab] = hexToRgb(a);
  const [br, bg, bb] = hexToRgb(b);
  return rgbToHex(ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t);
}

/** A palette colour at a continuous position, wrapping around the list. */
function samplePalette(scene: SceneName, position: number): string {
  const palette = SCENES[scene];
  const n = palette.length;
  const p = ((position % n) + n) % n;
  const i = Math.floor(p);
  return mix(palette[i], palette[(i + 1) % n], p - i);
}

/**
 * Paint one frame of the generated scene onto a canvas, and report its edges.
 *
 * The frame is a moving diagonal gradient with a soft radial highlight — enough
 * structure that the four edges differ from each other, which is the point. If
 * every edge were the same colour the backlight would look like a plain colour
 * wash and the sync would be indistinguishable from `setColor`.
 */
export function paintFrame(
  ctx: CanvasRenderingContext2D,
  scene: SceneName,
  tick: number,
  width: number,
  height: number,
): MediaFrame {
  const t = tick / 60;

  const gradient = ctx.createLinearGradient(0, 0, width, height);
  for (let i = 0; i <= 4; i++) {
    gradient.addColorStop(i / 4, samplePalette(scene, t + i * 0.7));
  }
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, width, height);

  // A highlight that drifts, so the edges are not just a static ramp.
  const cx = width * (0.5 + 0.35 * Math.sin(t * 0.9));
  const cy = height * (0.5 + 0.3 * Math.cos(t * 1.3));
  const radial = ctx.createRadialGradient(cx, cy, 0, cx, cy,
    Math.max(width, height) * 0.55);
  radial.addColorStop(0, samplePalette(scene, t * 1.7 + 2));
  radial.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = radial;
  ctx.fillRect(0, 0, width, height);

  return { edges: edgeColors(ctx, width, height), luminance: 0 };
}

/**
 * Average the pixels along each screen edge.
 *
 * Averaged over a band rather than a single row of pixels: one row lands on
 * whatever the gradient happens to be at that exact line, which flickers. A band
 * of ~12% of the dimension is what consumer ambient-light systems use, and it
 * reads as a stable colour that still tracks the picture.
 */
export function edgeColors(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
): [string, string, string, string] {
  const bandY = Math.max(2, Math.floor(height * 0.12));
  const bandX = Math.max(2, Math.floor(width * 0.12));

  const mean = (x: number, y: number, w: number, h: number): string => {
    const { data } = ctx.getImageData(x, y, Math.max(1, w), Math.max(1, h));
    let r = 0;
    let g = 0;
    let b = 0;
    const pixels = data.length / 4;
    for (let i = 0; i < data.length; i += 4) {
      r += data[i];
      g += data[i + 1];
      b += data[i + 2];
    }
    return rgbToHex(r / pixels, g / pixels, b / pixels);
  };

  return [
    mean(0, 0, width, bandY),                     // top
    mean(width - bandX, 0, bandX, height),        // right
    mean(0, height - bandY, width, bandY),        // bottom
    mean(0, 0, bandX, height),                    // left
  ];
}

/** Mean luminance of the frame, 0..1 — drives a brightness follow. */
export function frameLuminance(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
): number {
  // Sampled at low resolution: a full readback every frame is the expensive part
  // of this whole component, and the average does not need every pixel.
  const { data } = ctx.getImageData(0, 0, width, height);
  let sum = 0;
  const step = 4 * 37; // a prime stride, so the sample is not aligned to the gradient
  let n = 0;
  for (let i = 0; i < data.length; i += step) {
    sum += 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
    n += 1;
  }
  return n ? sum / n / 255 : 0;
}

// ---------------------------------------------------------------------------
// Music
// ---------------------------------------------------------------------------

export interface BeatState {
  /** 0..1, peaks on the beat and decays between. */
  energy: number;
  /** Which beat of the bar, 0-indexed. */
  beat: number;
  bpm: number;
}

/**
 * A synthesised beat envelope at a given tempo.
 *
 * Deliberately not an audio API: nothing plays, so nothing needs a user gesture
 * or an autoplay exemption, and a scene that "starts the music" cannot silently
 * fail. What a light sync actually consumes from music is an energy envelope, and
 * that is computable from the clock.
 *
 * The envelope is a sharp attack with an exponential decay, plus a stronger
 * downbeat every fourth beat, so the lights pulse in a bar rather than a
 * monotonous flash.
 */
export function beatAt(elapsedMs: number, bpm: number): BeatState {
  const beatMs = 60_000 / bpm;
  const position = elapsedMs / beatMs;
  const beat = Math.floor(position) % 4;
  const phase = position - Math.floor(position);
  // Exponential decay from the attack, floored so the lights never go fully dark.
  const decay = Math.exp(-phase * 4.5);
  const accent = beat === 0 ? 1 : 0.72;
  return { energy: 0.18 + 0.82 * decay * accent, beat, bpm };
}

/** Segment colours for a music sync: the palette pulsed by the beat energy. */
export function musicColors(scene: SceneName, beat: BeatState,
                            count: number): string[] {
  const out: string[] = [];
  for (let i = 0; i < count; i++) {
    const base = samplePalette(scene, beat.beat + i / Math.max(1, count));
    // Mix toward black as the beat decays, so the pulse reads as brightness
    // rather than a hue change.
    out.push(mix('#000000', base, beat.energy));
  }
  return out;
}
