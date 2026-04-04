import React, { useEffect, useState, useRef } from 'react';
import { MqttClient } from '../mqtt/MqttClient';

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

const MODE_LABELS: Record<CookingMode, string> = {
  white_rice: 'White Rice',
  brown_rice: 'Brown Rice',
  porridge: 'Porridge',
  steam: 'Steam',
};

const RiceCooker: React.FC = () => {
  const [status, setStatus] = useState<CookerStatus>('idle');
  const [cookingMode, setCookingMode] = useState<CookingMode>('white_rice');
  const [timeRemaining, setTimeRemaining] = useState(0);
  const [temperature, setTemperature] = useState(25);
  const [keepWarm, setKeepWarm] = useState(false);
  const timerRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    if (status === 'cooking' && timeRemaining > 0) {
      timerRef.current = setInterval(() => {
        setTimeRemaining((prev) => {
          if (prev <= 1) {
            clearInterval(timerRef.current!);
            setStatus(keepWarm ? 'keep_warm' : 'done');
            setTemperature(keepWarm ? 65 : 25);
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
    setCookingMode(mode);
    setStatus('cooking');
    setTimeRemaining(COOK_TIMES[mode]);
    setTemperature(25);
  };

  const stopCooking = () => {
    if (timerRef.current) clearInterval(timerRef.current);
    setStatus('idle');
    setTimeRemaining(0);
  };

  // MQTT subscription
  useEffect(() => {
    const mqtt = MqttClient.getInstance();
    const topic = 'smarthome/rice_cooker/command';
    const handler = (_topic: string, payload: any) => {
      switch (payload.action) {
        case 'start':
          if (payload.mode) startCooking(payload.mode as CookingMode);
          break;
        case 'stop':
          stopCooking();
          break;
        case 'keepWarm':
          if (typeof payload.enabled === 'boolean') setKeepWarm(payload.enabled);
          break;
      }
    };
    mqtt.subscribe(topic, handler);
    return () => mqtt.unsubscribe(topic, handler);
  }, []);

  const formatTime = (seconds: number): string => {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  const statusLabels: Record<CookerStatus, string> = {
    idle: 'Idle',
    cooking: 'Cooking',
    keep_warm: 'Keep Warm',
    done: 'Done',
  };

  return (
    <div className="device-card">
      <div className="device-card-header">
        <h2>Rice Cooker</h2>
        <div className="device-status">
          <span className={`dot ${status !== 'idle' ? 'on' : 'off'}`} />
          {statusLabels[status]}
        </div>
      </div>
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
              {status === 'idle' ? 'Ready' : MODE_LABELS[cookingMode]}
            </div>
            <div className="timer-display">
              {status === 'cooking' ? formatTime(timeRemaining) : status === 'keep_warm' ? 'WARM' : status === 'done' ? 'DONE' : '--:--'}
            </div>
            <div className="temp-display">{temperature}°C</div>
          </div>
        </div>
        <div className="cooker-buttons">
          {(Object.keys(MODE_LABELS) as CookingMode[]).map((m) => (
            <button
              key={m}
              className={status === 'cooking' && cookingMode === m ? 'active' : ''}
              onClick={() => startCooking(m)}
              disabled={status === 'cooking'}
            >
              {MODE_LABELS[m]}
            </button>
          ))}
          <button onClick={stopCooking} disabled={status === 'idle'}>
            Stop
          </button>
          <button
            className={keepWarm ? 'active' : ''}
            onClick={() => setKeepWarm(!keepWarm)}
          >
            Keep Warm: {keepWarm ? 'ON' : 'OFF'}
          </button>
        </div>
      </div>
    </div>
  );
};

export default RiceCooker;
