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
 * Fan with its spinning-blade visual, driven by the catalog.
 *
 * Speed went from 0-3 to 0-8 because the requirements use "set the fan to 8" as
 * an example; the range comes from the catalog so the buttons and the Lambda's
 * clamp agree. Blade speed is computed from the ratio rather than a per-step
 * lookup table, which would need re-writing on every range change.
 */
const Fan: React.FC<Props> = ({ device, userSub }) => {
  const { t } = useI18n();
  const { state, set, merge } = useDeviceState(device, userSub);
  const [timer, setTimer] = useState(0);

  const speedCap = device.capabilities.speed;
  const maxSpeed = speedCap?.max ?? 8;
  const power = Boolean(state.power);
  const speed = Number(state.speed ?? 0);
  const oscillation = Boolean(state.oscillation);

  const setPower = (v: boolean) => set('power', v);
  const setSpeed = (v: number) => set('speed', v);
  const setOscillation = (v: boolean) => set('oscillation', v);

  // Faster spin at higher settings, with a floor so step 1 still reads as motion.
  const spinDuration = speed > 0 ? `${(3.2 - (speed / maxSpeed) * 2.6).toFixed(2)}s` : '0s';

  // Timer countdown
  useEffect(() => {
    if (timer > 0 && power) {
      const interval = setInterval(() => {
        setTimer((prev) => {
          if (prev <= 1) {
            merge({ power: false, speed: 0 });
            return 0;
          }
          return prev - 1;
        });
      }, 1000);
      return () => clearInterval(interval);
    }
  }, [timer, power]);

  const togglePower = () => {
    merge(power ? { power: false, speed: 0 } : { power: true, speed: 1 });
  };

  const changeSpeed = (next: number) => {
    merge({ speed: next, power: next > 0 });
  };

  const formatTime = (seconds: number): string => {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  return (
    <DeviceCard
      device={device}
      status={power ? `${t('fan.speed')} ${speed}/${maxSpeed}` : t('common.off')}
      active={power}
    >
      <div className="fan-body">
        <div className="fan-visual" style={oscillation && power ? { animation: 'fan-osc 4s ease-in-out infinite' } : {}}>
          <style>{`
            @keyframes fan-osc {
              0%, 100% { transform: rotate(-15deg); }
              50% { transform: rotate(15deg); }
            }
          `}</style>
          <div className="fan-guard">
            <div className="fan-hub" />
            <div
              className={`fan-blades ${power && speed > 0 ? 'spinning' : ''}`}
              style={{ '--spin-duration': spinDuration } as React.CSSProperties}
            >
              <div className="fan-blade" />
              <div className="fan-blade" />
              <div className="fan-blade" />
              <div className="fan-blade" />
            </div>
          </div>
        </div>
        <div className="fan-stand" />
        <div className="fan-base" />
        <div className="fan-controls">
          <button
            className={power ? 'active' : ''}
            onClick={togglePower}
            style={power ? { background: '#1a3a1a', borderColor: '#22c55e', color: '#22c55e' } : {}}
          >
            {power ? t('common.on') : t('common.off')}
          </button>
          {/* Speeds start at 1: level 0 IS off, and rendering it as a second
              button left the card with two OFFs side by side. Highlighting is
              gated on power so a stored speed does not look active while the fan
              is off. */}
          {Array.from({ length: maxSpeed }, (_, i) => i + 1).map((level) => (
            <button
              key={level}
              className={power && speed === level ? 'active' : ''}
              onClick={() => changeSpeed(level)}
            >
              {level}
            </button>
          ))}
          <button
            className={oscillation ? 'active' : ''}
            onClick={() => setOscillation(!oscillation)}
          >
            {t('fan.oscillate')} {oscillation ? t('common.on') : t('common.off')}
          </button>
        </div>
        <div className="fan-info">
          {timer > 0 && <span>{t('fan.timer')} {formatTime(timer)}</span>}
          {oscillation && <span>{t('fan.oscillating')}</span>}
        </div>
      </div>
    </DeviceCard>
  );
};

export default Fan;
