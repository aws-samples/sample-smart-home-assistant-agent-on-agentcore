/**
 * A virtual clock with a speed multiplier, and the single time source the
 * simulator reads.
 *
 * Scheduled scenes are now real: a scene saved for 07:30 is a cron schedule in
 * EventBridge Scheduler and it fires at 07:30 UTC. That is correct and
 * undemonstrable — nobody watches a demo until tomorrow morning. The clock here
 * lets the simulator's own time run fast, so the diurnal sensor curve and the
 * on-screen time advance at 60x and a scene's effect can be seen within a minute.
 *
 * What it deliberately does NOT do is move the trigger. EventBridge fires on real
 * time, in AWS, and nothing in a browser can change that; pretending otherwise
 * would be the demo lying about the feature. What accelerating buys is the
 * *observable context*: the sensor history that a threshold trigger reads, and a
 * visible clock that makes "every day at 23:00" concrete. To fire a time-triggered
 * scene now, invoke it — the runner is idempotent and takes the same payload
 * Scheduler sends.
 *
 * Time is read through `now()` rather than `Date.now()` everywhere in the
 * simulator, so a single multiplier change moves every consumer together. A
 * component that keeps calling `Date.now()` would drift away from the rest of the
 * UI as soon as the multiplier is anything but 1, and the mismatch is subtle:
 * everything still animates, just against a different clock.
 */

export type ClockListener = (state: ClockState) => void;

export interface ClockState {
  /** Virtual epoch milliseconds. */
  nowMs: number;
  multiplier: number;
  /** True when the clock has been moved off real time in any way. */
  simulated: boolean;
}

/** Multipliers the UI offers. 1 is real time; 3600 makes an hour pass a second. */
export const MULTIPLIERS = [1, 10, 60, 600, 3600] as const;

const REAL = () => Date.now();

// The anchor pair: at real time `realAnchorMs`, the virtual clock read
// `virtualAnchorMs`. Virtual time is then a linear function of real time, so
// changing the multiplier re-anchors rather than jumping — otherwise every speed
// change would teleport the clock and break any elapsed-time arithmetic.
let realAnchorMs = REAL();
let virtualAnchorMs = realAnchorMs;
let multiplier = 1;
let offsetMs = 0;

const listeners = new Set<ClockListener>();

function snapshot(): ClockState {
  return { nowMs: nowMs(), multiplier, simulated: multiplier !== 1 || offsetMs !== 0 };
}

function emit(): void {
  const state = snapshot();
  listeners.forEach((fn) => fn(state));
}

/** Virtual epoch milliseconds. The only clock the simulator should read. */
export function nowMs(): number {
  return virtualAnchorMs + (REAL() - realAnchorMs) * multiplier + offsetMs;
}

/** Virtual epoch SECONDS, which is what the sensor history is keyed on. */
export function nowSeconds(): number {
  return Math.floor(nowMs() / 1000);
}

export function getMultiplier(): number {
  return multiplier;
}

export function isSimulated(): boolean {
  return multiplier !== 1 || offsetMs !== 0;
}

/**
 * Change the speed of virtual time.
 *
 * Re-anchors first, so time already elapsed keeps its old rate and only the
 * future runs at the new one. Without that, raising the multiplier would
 * retroactively multiply the whole session and throw the clock years out.
 */
export function setMultiplier(next: number): void {
  const current = nowMs();
  virtualAnchorMs = current - offsetMs;
  realAnchorMs = REAL();
  multiplier = Math.max(1, next);
  emit();
}

/** Jump the clock forward (or back) by a number of virtual minutes. */
export function skipMinutes(minutes: number): void {
  offsetMs += minutes * 60_000;
  emit();
}

/** Back to real time, discarding the offset. */
export function reset(): void {
  realAnchorMs = REAL();
  virtualAnchorMs = realAnchorMs;
  multiplier = 1;
  offsetMs = 0;
  emit();
}

export function subscribe(listener: ClockListener): () => void {
  listeners.add(listener);
  listener(snapshot());
  return () => {
    listeners.delete(listener);
  };
}

/** `HH:MM` in UTC — the same clock a scene's time trigger is stored against. */
export function formatTime(ms: number = nowMs()): string {
  const d = new Date(ms);
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

export function formatDateTime(ms: number = nowMs()): string {
  return new Date(ms).toISOString().replace('T', ' ').slice(0, 19) + 'Z';
}
