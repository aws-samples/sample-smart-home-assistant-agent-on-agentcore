import React, { useEffect, useState, useRef } from 'react';
import { DeviceCard } from './DeviceCard';
import { DeviceDef } from '../devices/catalog';
import { useDeviceState } from '../state/useDeviceState';
import { useI18n } from '../i18n';

type CookerStatus = 'idle' | 'cooking' | 'keep_warm' | 'done';
type CookingMode = 'white_rice' | 'brown_rice' | 'porridge' | 'steam';

const COOK_TIMES: Record<CookingMode, number> = {
  white_rice: 1200,
  brown_rice: 1800,
  porridge: 900,
  steam: 600,
};

const TARGET_TEMPS: Record<CookingMode, number> = {
  white_rice: 100,
  brown_rice: 100,
  porridge: 95,
  steam: 100,
};

const MODE_LABEL_KEYS: Record<CookingMode, string> = {
  white_rice: 'rice.mode.whiteRice',
  brown_rice: 'rice.mode.brownRice',
  porridge: 'rice.mode.porridge',
  steam: 'rice.mode.steam',
};

const STATUS_LABEL_KEYS: Record<CookerStatus, string> = {
  idle: 'rice.status.idle',
  cooking: 'rice.status.cooking',
  keep_warm: 'rice.status.keepWarm',
  done: 'rice.status.done',
};

interface Props {
  device: DeviceDef;
  userSub: string;
}

/**
 * Rice cooker, keeping its steam and display visual.
 *
 * `cooking`, `mode` and `keep_warm` are catalog-backed. The four-way `status`
 * (idle / cooking / keep_warm / done) stays local and derived: it encodes where a
 * cook cycle got to, which is presentation, and 'done' in particular is not a
 * commandable state — an agent can start or stop a cooker, not put it in 'done'.
 */
const RiceCooker: React.FC<Props> = ({ device, userSub }) => {
  const { t } = useI18n();
  const { state, set, merge } = useDeviceState(device, userSub);
  const [status, setStatus] = useState<CookerStatus>('idle');
  const [timeRemaining, setTimeRemaining] = useState(0);
  const [temperature, setTemperature] = useState(25);
  const timerRef = useRef<NodeJS.Timeout | null>(null);

  const cookingMode = String(state.mode ?? 'white_rice') as CookingMode;
  const keepWarm = Boolean(state.keep_warm);
  const setKeepWarm = (v: boolean) => set('keep_warm', v);

  // The catalog's `cooking` boolean is the wire-level truth; the richer local
  // status follows it, so an agent's start/stop drives the visual too.
  useEffect(() => {
    const cooking = Boolean(state.cooking);
    if (cooking && status !== 'cooking') {
      setStatus('cooking');
      setTimeRemaining(COOK_TIMES[cookingMode]);
      setTemperature(25);
    } else if (!cooking && status === 'cooking') {
      setStatus('idle');
      setTimeRemaining(0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.cooking, state.mode]);

  useEffect(() => {
    if (status === 'cooking' && timeRemaining > 0) {
      timerRef.current = setInterval(() => {
        setTimeRemaining((prev) => {
          if (prev <= 1) {
            clearInterval(timerRef.current!);
            setStatus(keepWarm ? 'keep_warm' : 'done');
            setTemperature(keepWarm ? 65 : 25);
            set('cooking', false);
            return 0;
          }
          return prev - 1;
        });
        setTemperature((prev) => {
          const target = TARGET_TEMPS[cookingMode];
          if (prev < target) return Math.min(prev + 2, target);
          return target;
        });
      }, 1000);
      return () => { if (timerRef.current) clearInterval(timerRef.current); };
    }
  }, [status, timeRemaining, cookingMode, keepWarm]);

  // Cool down when idle
  useEffect(() => {
    if (status === 'idle' && temperature > 25) {
      const interval = setInterval(() => {
        setTemperature((prev) => {
          if (prev <= 25) { clearInterval(interval); return 25; }
          return prev - 1;
        });
      }, 500);
      return () => clearInterval(interval);
    }
  }, [status, temperature]);

  const startCooking = (mode: CookingMode) => {
    merge({ mode, cooking: true });
  };

  const stopCooking = () => {
    if (timerRef.current) clearInterval(timerRef.current);
    set('cooking', false);
  };

  const formatTime = (seconds: number): string => {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  return (
    <DeviceCard
      device={device}
      status={t(STATUS_LABEL_KEYS[status])}
      active={status !== 'idle'}
    >
      <div className="rice-cooker-body">
        <div className="cooker-visual">
          {status === 'cooking' && (
            <div className="cooker-steam">
              <div className="steam-line" />
              <div className="steam-line" />
              <div className="steam-line" />
            </div>
          )}
          <div className="cooker-lid" />
          <div className={`cooker-display ${status === 'cooking' ? 'cooking' : ''}`}>
            <div className="mode-label">
              {status === 'idle' ? t('rice.ready') : t(MODE_LABEL_KEYS[cookingMode])}
            </div>
            <div className="timer-display">
              {status === 'cooking' ? formatTime(timeRemaining) : status === 'keep_warm' ? t('rice.warm') : status === 'done' ? t('rice.done') : '--:--'}
            </div>
            <div className="temp-display">{temperature}°C</div>
          </div>
        </div>
        <div className="cooker-buttons">
          {(Object.keys(MODE_LABEL_KEYS) as CookingMode[]).map((m) => (
            <button
              key={m}
              className={status === 'cooking' && cookingMode === m ? 'active' : ''}
              onClick={() => startCooking(m)}
              disabled={status === 'cooking'}
            >
              {t(MODE_LABEL_KEYS[m])}
            </button>
          ))}
          <button onClick={stopCooking} disabled={status === 'idle'}>
            {t('rice.stop')}
          </button>
          <button
            className={keepWarm ? 'active' : ''}
            onClick={() => setKeepWarm(!keepWarm)}
          >
            {t('rice.keepWarm')} {keepWarm ? t('common.on') : t('common.off')}
          </button>
        </div>
      </div>
    </DeviceCard>
  );
};

export default RiceCooker;
