/**
 * Effect renderer checks — run with: npx tsx scripts/verify-effects.ts
 *
 * effects.ts is pure math with several things that are easy to get subtly wrong
 * and hard to notice in a UI: hue interpolation taking the long way around the
 * color wheel, count=1 edge cases, sparkle re-randomising every frame instead of
 * holding, brightness not scaling monotonically. Standalone rather than a jest
 * suite because the simulator has no test framework and adding one is not in
 * this change's scope.
 */
import { renderFrame, sampleRing, hexToHsl, hslToHex, kelvinToHex, MODE_PRESETS, EffectName } from '../src/devices/effects';

let fail = 0;
const check = (name: string, cond: boolean, extra = '') => {
  if (!cond) { console.log(`FAIL ${name} ${extra}`); fail++; } else { console.log(`ok   ${name}`); }
};
const isHex = (s: string) => /^#[0-9a-f]{6}$/.test(s);

// round-trip
for (const hex of ['#ff0000', '#00ff00', '#0000ff', '#ffffff', '#000000', '#0c4a6e', '#f472b6']) {
  const back = hslToHex(hexToHsl(hex));
  const d = [1,3,5].map(i => Math.abs(parseInt(hex.slice(i,i+2),16) - parseInt(back.slice(i,i+2),16)));
  check(`roundtrip ${hex}`, Math.max(...d) <= 2, `-> ${back}`);
}

// hue takes the short way: red(0) -> magenta(300) must pass through ~330, not ~150
const mid = sampleRing([hexToHsl('#ff0000'), hexToHsl('#ff00ff')], 0.25);
check('hue short path red->magenta', mid.h > 300 || mid.h < 30, `h=${mid.h.toFixed(1)}`);

// ring wraps: t=0 and t=1 identical
const a = sampleRing([hexToHsl('#ff0000'), hexToHsl('#00ff00')], 0);
const b = sampleRing([hexToHsl('#ff0000'), hexToHsl('#00ff00')], 1);
check('ring wraps at t=1', Math.abs(a.h-b.h) < 0.001 && Math.abs(a.l-b.l) < 0.001);

// every effect: valid hex, correct length, at any count
const effects: EffectName[] = ['solid','gradient','wave','chase','breathe','sparkle','twinkle','flicker'];
for (const effect of effects) {
  for (const count of [1, 30, 256]) {
    let allValid = true, anyLit = false;
    for (let tick = 0; tick < 40; tick++) {
      const f = renderFrame({ effect, colors: ['#0c4a6e','#22d3ee','#a5f3fc'], speed: 5, brightness: 100 }, count, tick);
      if (f.length !== count) { allValid = false; break; }
      for (const c of f) { if (!isHex(c)) { allValid = false; } if (c !== '#000000') anyLit = true; }
    }
    check(`${effect} count=${count} valid+lit`, allValid && anyLit);
  }
}

// empty palette must not crash or go black
const empty = renderFrame({ effect: 'gradient', colors: [], speed: 5, brightness: 100 }, 10, 0);
check('empty palette falls back', empty.every(isHex) && empty.some(c => c !== '#000000'));

// brightness 0 -> black; brightness scales monotonically
const dark = renderFrame({ effect: 'solid', colors: ['#ffffff'], speed: 1, brightness: 0 }, 5, 0);
check('brightness 0 is black', dark.every(c => c === '#000000'));
const lo = renderFrame({ effect: 'solid', colors: ['#ffffff'], speed: 1, brightness: 30 }, 5, 0)[0];
const hi = renderFrame({ effect: 'solid', colors: ['#ffffff'], speed: 1, brightness: 90 }, 5, 0)[0];
check('brightness monotonic', parseInt(lo.slice(1,3),16) < parseInt(hi.slice(1,3),16), `${lo} < ${hi}`);

// sparkle is deterministic for the same tick (no per-frame restrobe)
const s1 = renderFrame({ effect: 'sparkle', colors: ['#ffffff'], speed: 5, brightness: 100 }, 30, 7);
const s2 = renderFrame({ effect: 'sparkle', colors: ['#ffffff'], speed: 5, brightness: 100 }, 30, 7);
check('sparkle deterministic', JSON.stringify(s1) === JSON.stringify(s2));

// animated effects actually change over time
for (const effect of ['wave','chase','breathe','twinkle','flicker'] as EffectName[]) {
  const f0 = renderFrame({ effect, colors: ['#ff0000','#00ff00'], speed: 8, brightness: 100 }, 30, 0);
  const f9 = renderFrame({ effect, colors: ['#ff0000','#00ff00'], speed: 8, brightness: 100 }, 30, 25);
  check(`${effect} animates`, JSON.stringify(f0) !== JSON.stringify(f9));
}

// gradient is static (it is a spread, not a motion)
const g0 = renderFrame({ effect: 'gradient', colors: ['#ff0000','#0000ff'], speed: 5, brightness: 100 }, 30, 0);
const g9 = renderFrame({ effect: 'gradient', colors: ['#ff0000','#0000ff'], speed: 5, brightness: 100 }, 30, 99);
check('gradient is static', JSON.stringify(g0) === JSON.stringify(g9));

// every legacy led mode has a preset that renders
for (const mode of ['rainbow','breathing','chase','sparkle','fire','ocean','aurora','solid']) {
  const p = MODE_PRESETS[mode];
  check(`preset ${mode} exists`, !!p);
  if (p) {
    const f = renderFrame({ ...p, brightness: 80 }, 256, 5);
    check(`preset ${mode} renders`, f.length === 256 && f.every(isHex) && f.some(c => c !== '#000000'));
  }
}

// kelvin: warm is redder than cool
const warm = kelvinToHex(2200), cool = kelvinToHex(6500);
check('kelvin warm is redder', parseInt(warm.slice(5,7),16) < parseInt(cool.slice(5,7),16), `${warm} vs ${cool}`);
check('kelvin clamps', isHex(kelvinToHex(-5)) && isHex(kelvinToHex(99999)));

console.log(fail === 0 ? '\nALL PASS' : `\n${fail} FAILURES`);
process.exit(fail === 0 ? 0 : 1);
