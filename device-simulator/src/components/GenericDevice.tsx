import React, { useEffect, useState } from 'react';
import { DeviceCard } from './DeviceCard';
import { DeviceDef } from '../devices/catalog';
import { useDeviceState } from '../state/useDeviceState';
import { useI18n } from '../i18n';

interface Props {
  device: DeviceDef;
  userSub: string;
}

/**
 * Controls rendered from a device's declared capabilities.
 *
 * Covers the devices without a bespoke visual (plug, humidifier, purifier, ice
 * maker): a boolean gets a toggle, an integer gets a level meter, an enum gets a
 * button row, and a readonly capability gets a gauge. Adding a device to the
 * catalog therefore gives it a working card with correctly-bounded controls and
 * no new component — and the bounds shown are the ones the Lambda enforces, so
 * the UI cannot offer a value the backend would clamp.
 *
 * Integers render as a segmented meter rather than a bare `<input type=range>`:
 * a mist level of 3-of-5 is a discrete setting, and stepped bars read as a
 * device's own control panel where a slider reads as a form field.
 */
export function GenericDevice({ device, userSub }: Props) {
  const { t } = useI18n();
  const { state, set } = useDeviceState(device, userSub);

  const entries = Object.entries(device.capabilities);
  const readonlyFields = entries.filter(([, cap]) => cap.type === 'readonly');
  const controlFields = entries.filter(([, cap]) => cap.type !== 'readonly');

  // The first boolean is the device's "is it doing anything" signal: `power` on
  // most, `making_ice` on the ice maker.
  const primary = controlFields.find(([, cap]) => cap.type === 'boolean')?.[0];
  const active = primary ? Boolean(state[primary]) : false;

  // Consumables drain while the device runs, so "check the water level" and
  // "when do I change the filter" have a moving answer rather than a constant.
  const [readings, setReadings] = useState<Record<string, number>>(() => {
    const seed: Record<string, number> = {};
    for (const [field] of readonlyFields) {
      seed[field] = field === 'bin_level' ? 35 : 82;
    }
    return seed;
  });

  useEffect(() => {
    if (readonlyFields.length === 0) return;
    const timer = setInterval(() => {
      setReadings((prev) => {
        const next = { ...prev };
        for (const [field] of readonlyFields) {
          const current = prev[field] ?? 80;
          // An ice bin fills as it runs; water and filter life deplete.
          const rising = field === 'bin_level';
          const drift = active ? (rising ? 1.5 : -0.8) : 0;
          next[field] = Math.max(0, Math.min(100, Math.round((current + drift) * 10) / 10));
        }
        return next;
      });
    }, 15_000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, device.deviceId]);

  const statusParts: string[] = [];
  if (active) {
    for (const [field, cap] of controlFields) {
      if (field === primary) continue;
      if (cap.type === 'enum' && state[field]) statusParts.push(label(t, `mode.${state[field]}`, String(state[field])));
      if (cap.type === 'integer' && typeof state[field] === 'number') {
        statusParts.push(`${state[field]}${cap.unit ?? ''}`);
      }
    }
  }
  const status = active ? (statusParts.join(' · ') || t('common.on')) : t('common.off');

  return (
    <DeviceCard device={device} status={status} active={active}>
      {readonlyFields.length > 0 && (
        <div className="gauge-row">
          {readonlyFields.map(([field, cap]) => {
            const value = readings[field] ?? 0;
            const low = field === 'bin_level' ? value > 90 : value < 20;
            return (
              <div className="gauge" key={field}>
                <div className="gauge-head">
                  <span>{label(t, `cap.${field}`, field)}</span>
                  <span className={`gauge-value ${low ? 'gauge-warn' : ''}`}>
                    {value}{cap.unit ?? ''}
                  </span>
                </div>
                <div className="gauge-track">
                  <div
                    className={`gauge-fill ${low ? 'gauge-fill-warn' : ''}`}
                    style={{ width: `${value}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div className="generic-controls">
        {controlFields.map(([field, cap]) => {
          const value = state[field];

          if (cap.type === 'boolean') {
            const on = Boolean(value);
            return (
              <div className="control-row" key={field}>
                <span className="control-label">{label(t, `cap.${field}`, field)}</span>
                <button
                  className={`pill ${on ? 'pill-on' : ''}`}
                  onClick={() => set(field, !on)}
                >
                  {on ? t('common.on') : t('common.off')}
                </button>
              </div>
            );
          }

          if (cap.type === 'integer') {
            const min = cap.min ?? 0;
            const max = cap.max ?? 100;
            const num = typeof value === 'number' ? value : min;
            const steps = max - min + 1;
            // A short range is a set of discrete steps (mist level 1-5); a wide
            // one is a continuous quantity (target humidity 30-80) and gets a
            // slider, because 51 segments is not a meter.
            if (steps <= 10) {
              return (
                <div className="control-row" key={field}>
                  <span className="control-label">{label(t, `cap.${field}`, field)}</span>
                  <div className="meter">
                    {Array.from({ length: steps }, (_, i) => min + i).map((level) => (
                      <button
                        key={level}
                        className={`meter-seg ${active && num >= level && level > min ? 'meter-seg-on' : ''}`}
                        title={String(level)}
                        onClick={() => set(field, level)}
                      />
                    ))}
                  </div>
                  <span className="control-value">{num}{cap.unit ?? ''}</span>
                </div>
              );
            }
            return (
              <div className="control-row" key={field}>
                <span className="control-label">{label(t, `cap.${field}`, field)}</span>
                <input
                  type="range"
                  min={min}
                  max={max}
                  value={num}
                  onChange={(e) => set(field, Number(e.target.value))}
                  style={{ flex: 1, accentColor: '#0972d3' }}
                />
                <span className="control-value">{num}{cap.unit ?? ''}</span>
              </div>
            );
          }

          if (cap.type === 'enum') {
            return (
              <div className="control-row control-row-wrap" key={field}>
                <span className="control-label">{label(t, `cap.${field}`, field)}</span>
                <div className="control-buttons">
                  {(cap.values ?? []).map((option) => (
                    <button
                      key={option}
                      className={`pill ${value === option ? 'pill-sel' : ''}`}
                      onClick={() => set(field, option)}
                    >
                      {label(t, `mode.${option}`, option)}
                    </button>
                  ))}
                </div>
              </div>
            );
          }

          return null;
        })}
      </div>
    </DeviceCard>
  );
}

/** Translate, falling back to the raw key name when no string is defined. */
function label(t: (k: string) => string, key: string, fallback: string): string {
  const translated = t(key);
  return translated === key ? fallback : translated;
}
