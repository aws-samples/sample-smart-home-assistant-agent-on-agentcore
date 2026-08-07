import React from 'react';
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
 * Covers the non-visual devices (plug, fan, cooker, oven): a boolean gets a
 * toggle, an integer gets a slider, an enum gets a button row. Adding a device
 * to the catalog therefore gives it a working card with correctly-bounded
 * controls and no new component — and the bounds shown are the same ones the
 * Lambda enforces, so the UI cannot offer a value the backend would clamp.
 *
 * Devices with a distinctive look (lights, sensors) have their own components;
 * this is the fallback that keeps the catalog open-ended.
 */
export function GenericDevice({ device, userSub }: Props) {
  const { t } = useI18n();
  const { state, set } = useDeviceState(device, userSub);

  // The first boolean capability is the device's "is it doing anything" signal:
  // `power` on most, `cooking` on the rice cooker.
  const primary = Object.entries(device.capabilities)
    .find(([, cap]) => cap.type === 'boolean')?.[0];
  const active = primary ? Boolean(state[primary]) : false;

  const statusParts: string[] = [];
  for (const [field, cap] of Object.entries(device.capabilities)) {
    if (field === primary || !active) continue;
    if (cap.type === 'enum' && state[field]) statusParts.push(String(state[field]));
    if (cap.type === 'integer' && typeof state[field] === 'number') {
      statusParts.push(`${state[field]}${cap.unit ?? ''}`);
    }
  }
  const status = active ? (statusParts.join(' · ') || t('common.on')) : t('common.off');

  return (
    <DeviceCard device={device} status={status} active={active}>
      <div className="generic-controls">
        {Object.entries(device.capabilities).map(([field, cap]) => {
          const value = state[field];

          if (cap.type === 'boolean') {
            const on = Boolean(value);
            return (
              <div className="control-row" key={field}>
                <span className="control-label">{t(`cap.${field}`) === `cap.${field}` ? field : t(`cap.${field}`)}</span>
                <button
                  className={on ? 'active' : ''}
                  onClick={() => set(field, !on)}
                  style={on ? { background: '#1a3a1a', borderColor: '#22c55e', color: '#22c55e' } : {}}
                >
                  {on ? t('common.on') : t('common.off')}
                </button>
              </div>
            );
          }

          if (cap.type === 'integer') {
            const num = typeof value === 'number' ? value : (cap.min ?? 0);
            return (
              <div className="control-row" key={field}>
                <span className="control-label">{t(`cap.${field}`) === `cap.${field}` ? field : t(`cap.${field}`)}</span>
                <input
                  type="range"
                  min={cap.min ?? 0}
                  max={cap.max ?? 100}
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
                <span className="control-label">{t(`cap.${field}`) === `cap.${field}` ? field : t(`cap.${field}`)}</span>
                <div className="control-buttons">
                  {(cap.values ?? []).map((option) => (
                    <button
                      key={option}
                      className={value === option ? 'active' : ''}
                      onClick={() => set(field, option)}
                    >
                      {t(`mode.${option}`) === `mode.${option}` ? option : t(`mode.${option}`)}
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
