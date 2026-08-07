import React, { useEffect, useRef, useState } from 'react';
import { DeviceCard } from './DeviceCard';
import { DeviceDef } from '../devices/catalog';
import { useDeviceState } from '../state/useDeviceState';
import { EffectName, MODE_PRESETS, kelvinToHex, renderFrame } from '../devices/effects';
import { useI18n } from '../i18n';

interface Props {
  device: DeviceDef;
  userSub: string;
}

/**
 * Lights: addressable strips, plain bulbs, and the LED matrix.
 *
 * All three are the same device as far as rendering goes — N elements, each
 * given a colour by the effect engine. The strip has 30 segments in a row, the
 * matrix is 256 in a grid, a bulb is one. Driving them from one component is
 * what makes a generated effect show up identically on any of them, instead of
 * only on whichever device someone wrote a branch for.
 */
export function LightDevice({ device, userSub }: Props) {
  const { t } = useI18n();
  const { state, set, merge } = useDeviceState(device, userSub);
  const caps = device.capabilities;

  const power = Boolean(state.power);
  const brightness = Number(state.brightness ?? 80);
  const segmentCount = caps.segments?.count ?? (device.deviceType === 'led_matrix' ? 256 : 1);
  const isMatrix = device.deviceType === 'led_matrix';

  // `effect` is the strip's own capability; the matrix expresses the same thing
  // as `mode`, which maps onto a preset.
  const effectName = (state.effect as EffectName | undefined)
    ?? (MODE_PRESETS[String(state.mode ?? '')]?.effect)
    ?? 'solid';
  const presetColors = MODE_PRESETS[String(state.mode ?? '')]?.colors;
  const explicitColors = Array.isArray(state.segments) && (state.segments as string[]).length
    ? (state.segments as string[])
    : undefined;
  const singleColor = typeof state.color === 'string' ? (state.color as string) : undefined;
  const tempColor = state.color_temp !== undefined && !singleColor
    ? kelvinToHex(Number(state.color_temp))
    : undefined;

  const colors = explicitColors
    ?? presetColors
    ?? (singleColor ? [singleColor] : undefined)
    ?? (tempColor ? [tempColor] : ['#ffffff']);

  const speed = Number(state.effect_speed ?? MODE_PRESETS[String(state.mode ?? '')]?.speed ?? 5);

  const [pixels, setPixels] = useState<string[]>(() => new Array(segmentCount).fill('#000000'));
  const tickRef = useRef(0);
  const frameRef = useRef(0);

  // The animation loop reads its inputs from a ref so changing colour does not
  // tear down and restart the loop mid-frame.
  const specRef = useRef({ effectName, colors, speed, brightness, power, segmentCount });
  specRef.current = { effectName, colors, speed, brightness, power, segmentCount };

  useEffect(() => {
    let running = true;
    const animate = () => {
      if (!running) return;
      const s = specRef.current;
      tickRef.current += 1;
      setPixels(
        s.power
          ? renderFrame(
              { effect: s.effectName, colors: s.colors, speed: s.speed, brightness: s.brightness },
              s.segmentCount,
              tickRef.current,
            )
          : new Array(s.segmentCount).fill('#000000'),
      );
      frameRef.current = requestAnimationFrame(animate);
    };
    frameRef.current = requestAnimationFrame(animate);
    return () => {
      running = false;
      cancelAnimationFrame(frameRef.current);
    };
  }, []);

  const effectOptions = caps.effect?.values ?? caps.mode?.values ?? [];
  const effectField = caps.effect ? 'effect' : 'mode';

  const statusText = power
    ? `${String(state.mode ?? state.effect ?? '')} ${brightness}%`.trim()
    : t('common.off');

  // A single-element light is a bulb, not a one-segment strip: stretched across
  // the card it reads as a grey bar rather than something that is lit.
  const isBulb = segmentCount === 1;

  return (
    <DeviceCard device={device} status={statusText} active={power}>
      {isBulb ? (
        <div className="bulb-wrap">
          <div
            className={`bulb ${power ? 'lit' : ''}`}
            style={power ? {
              backgroundColor: pixels[0],
              boxShadow: `0 0 18px ${pixels[0]}, 0 0 42px ${pixels[0]}66`,
            } : undefined}
          />
          <div className="bulb-base" />
        </div>
      ) : (
        <div className={isMatrix ? 'led-grid' : 'strip-row'}>
          {pixels.map((color, i) => (
            <div
              key={i}
              className={isMatrix ? 'led-pixel' : 'strip-segment'}
              style={{
                backgroundColor: color,
                boxShadow: power && color !== '#000000'
                  ? `0 0 4px ${color}, 0 0 8px ${color}88`
                  : 'none',
              }}
            />
          ))}
        </div>
      )}

      <div className="led-controls">
        <button
          className={power ? 'active' : ''}
          onClick={() => set('power', !power)}
          style={power ? { background: '#1a3a1a', borderColor: '#22c55e', color: '#22c55e' } : {}}
        >
          {power ? t('common.on') : t('common.off')}
        </button>
        {effectOptions.map((name) => (
          <button
            key={name}
            className={String(state[effectField]) === name && power ? 'active' : ''}
            onClick={() => merge({ [effectField]: name, power: true })}
          >
            {t(`effect.${name}`) === `effect.${name}` ? name : t(`effect.${name}`)}
          </button>
        ))}
      </div>

      <div className="led-info">
        {caps.brightness && (
          <span>
            {t('led.brightness')}
            <input
              type="range"
              min={caps.brightness.min ?? 0}
              max={caps.brightness.max ?? 100}
              value={brightness}
              onChange={(e) => set('brightness', Number(e.target.value))}
              style={{ width: 80, marginLeft: 4, accentColor: '#a78bfa' }}
            />
            {brightness}%
          </span>
        )}
        {caps.color_temp && (
          <span>
            {t('light.colorTemp')}
            <input
              type="range"
              min={caps.color_temp.min ?? 2000}
              max={caps.color_temp.max ?? 6500}
              step={100}
              value={Number(state.color_temp ?? 4000)}
              onChange={(e) => merge({ color_temp: Number(e.target.value), color: undefined })}
              style={{ width: 80, marginLeft: 4, accentColor: '#fbbf24' }}
            />
            {Number(state.color_temp ?? 4000)}K
          </span>
        )}
        {caps.color && (
          <span>
            {t('light.color')}
            <input
              type="color"
              value={singleColor ?? '#ffffff'}
              onChange={(e) => merge({ color: e.target.value, segments: [] })}
              style={{ marginLeft: 4, width: 32, height: 20, border: 'none', background: 'none' }}
            />
          </span>
        )}
      </div>
    </DeviceCard>
  );
}
