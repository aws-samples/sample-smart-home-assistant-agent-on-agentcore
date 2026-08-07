import React, { useEffect, useState, useRef } from 'react';
import { DeviceCard } from './DeviceCard';
import { DeviceDef } from '../devices/catalog';
import { useDeviceState } from '../state/useDeviceState';
import { useI18n } from '../i18n';

type OvenMode = 'off' | 'bake' | 'broil' | 'convection' | 'preheat';

const MODE_LABEL_KEYS: Record<OvenMode, string> = {
  off: 'oven.mode.off',
  bake: 'oven.mode.bake',
  broil: 'oven.mode.broil',
  convection: 'oven.mode.convection',
  preheat: 'oven.mode.preheat',
};

interface Props {
  device: DeviceDef;
  userSub: string;
}

/**
 * Oven, keeping its heating-element glow and window visual.
 *
 * Power, mode and target temperature are catalog-backed and reported to the
 * cloud. `currentTemp` stays local on purpose: it is a simulated physical
 * reading that ticks twice a second while the oven warms, and reporting it would
 * be a state write per tick for a value nothing queries — the oven is not one of
 * the sensors.
 */
const Oven: React.FC<Props> = ({ device, userSub }) => {
  const { t } = useI18n();
  const { state, set, merge } = useDeviceState(device, userSub);
  const [currentTemp, setCurrentTemp] = useState(72);
  const [timer, setTimer] = useState(0);
  const timerRef = useRef<NodeJS.Timeout | null>(null);

  const tempCap = device.capabilities.temperature;
  const power = Boolean(state.power);
  // `off` is a UI-only mode: the catalog models "not running" as power=false,
  // since an enum value of 'off' plus a power boolean can disagree.
  const mode = (power ? String(state.mode ?? 'bake') : 'off') as OvenMode;
  const targetTemp = Number(state.temperature ?? 350);

  const setPower = (v: boolean) => set('power', v);
  const setMode = (m: OvenMode) =>
    m === 'off' ? set('power', false) : merge({ mode: m, power: true });
  const setTargetTemp = (v: number) => set('temperature', v);

  // Temperature simulation
  useEffect(() => {
    if (power && mode !== 'off') {
      const interval = setInterval(() => {
        setCurrentTemp((prev) => {
          if (prev < targetTemp) {
            const diff = targetTemp - prev;
            const step = Math.max(1, Math.floor(diff * 0.05));
            return Math.min(prev + step, targetTemp);
          }
          if (prev > targetTemp) {
            return prev - 1;
          }
          // Fluctuate around target
          return prev + (Math.random() > 0.5 ? 1 : -1);
        });
      }, 500);
      return () => clearInterval(interval);
    } else if (!power || mode === 'off') {
      // Cool down
      const interval = setInterval(() => {
        setCurrentTemp((prev) => {
          if (prev <= 72) { clearInterval(interval); return 72; }
          return prev - 2;
        });
      }, 500);
      return () => clearInterval(interval);
    }
  }, [power, mode, targetTemp]);

  // Timer countdown
  useEffect(() => {
    if (timer > 0 && power) {
      timerRef.current = setInterval(() => {
        setTimer((prev) => {
          if (prev <= 1) {
            clearInterval(timerRef.current!);
            setPower(false);
            return 0;
          }
          return prev - 1;
        });
      }, 1000);
      return () => { if (timerRef.current) clearInterval(timerRef.current); };
    }
  }, [timer, power]);

  // Preheat -> bake transition
  useEffect(() => {
    if (mode === 'preheat' && currentTemp >= targetTemp - 5) {
      setMode('bake');
    }
  }, [mode, currentTemp, targetTemp]);

  const formatTime = (seconds: number): string => {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  const isHeating = power && mode !== 'off';
  const heatPercent = isHeating ? Math.min(100, ((currentTemp - 72) / (targetTemp - 72)) * 100) : 0;
  const isBottomHot = isHeating && (mode === 'bake' || mode === 'convection' || mode === 'preheat');
  const isTopHot = isHeating && (mode === 'broil' || mode === 'convection');

  return (
    <DeviceCard device={device} status={t(MODE_LABEL_KEYS[mode])} active={power}>
      <div className="oven-body">
        <div className="oven-visual">
          <div className="oven-control-panel">
            <div className="oven-knob" />
            <div className="oven-screen">
              <div className="oven-mode">{t(MODE_LABEL_KEYS[mode])}</div>
              <div className="oven-temp" style={{ color: isHeating ? '#ef4444' : '#666' }}>
                {currentTemp}°F
              </div>
              <div className="oven-timer">
                {timer > 0 ? formatTime(timer) : isHeating ? `${t('oven.target')} ${targetTemp}°F` : ''}
              </div>
            </div>
            <div className="oven-knob" />
          </div>
          <div className={`oven-window ${isHeating ? 'heating' : ''}`}>
            <div className={`oven-element-top ${isTopHot ? 'hot' : ''}`} />
            <div
              className="oven-glow"
              style={{ height: `${heatPercent}%` }}
            />
            <div className={`oven-element ${isBottomHot ? 'hot' : ''}`} />
          </div>
        </div>
        <div className="oven-controls">
          <button
            className={power ? 'active' : ''}
            onClick={() => setMode(power ? 'off' : 'preheat')}
            style={power ? { background: '#3a1a1a', borderColor: '#ef4444', color: '#ef4444' } : {}}
          >
            {power ? t('common.on') : t('common.off')}
          </button>
          {(['bake', 'broil', 'convection'] as OvenMode[]).map((m) => (
            <button
              key={m}
              className={mode === m ? 'active' : ''}
              onClick={() => setMode(m)}
            >
              {t(MODE_LABEL_KEYS[m])}
            </button>
          ))}
        </div>
        <div className="oven-info" style={{ flexDirection: 'column', alignItems: 'center', gap: 8 }}>
          <span>
            {t('oven.temperature')}
            <input
              type="range"
              min={tempCap?.min ?? 200}
              max={tempCap?.max ?? 500}
              step={25}
              value={targetTemp}
              onChange={(e) => setTargetTemp(Number(e.target.value))}
              style={{ width: 100, marginLeft: 6, accentColor: '#ef4444' }}
            />
            {targetTemp}°F
          </span>
          {timer > 0 && <span>{t('oven.timer')} {formatTime(timer)}</span>}
        </div>
      </div>
    </DeviceCard>
  );
};

export default Oven;
