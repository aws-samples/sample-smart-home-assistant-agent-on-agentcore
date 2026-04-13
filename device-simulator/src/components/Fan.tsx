import React, { useEffect, useState } from 'react';
import { MqttClient } from '../mqtt/MqttClient';
import { useI18n } from '../i18n';

const SPEED_LABEL_KEYS = ['fan.speed.off', 'fan.speed.low', 'fan.speed.medium', 'fan.speed.high'];
const SPIN_DURATIONS = ['0s', '3s', '1.5s', '0.6s'];

const Fan: React.FC = () => {
  const [power, setPower] = useState(false);
  const [speed, setSpeed] = useState(0);
  const [oscillation, setOscillation] = useState(false);
  const [timer, setTimer] = useState(0);
  const { t } = useI18n();

  // Timer countdown
  useEffect(() => {
    if (timer > 0 && power) {
      const interval = setInterval(() => {
        setTimer((prev) => {
          if (prev <= 1) {
            setPower(false);
            setSpeed(0);
            return 0;
          }
          return prev - 1;
        });
      }, 1000);
      return () => clearInterval(interval);
    }
  }, [timer, power]);

  const togglePower = () => {
    if (power) {
      setPower(false);
      setSpeed(0);
    } else {
      setPower(true);
      setSpeed(1);
    }
  };

  const changeSpeed = (s: number) => {
    setSpeed(s);
    if (s > 0 && !power) setPower(true);
    if (s === 0) setPower(false);
  };

  // MQTT subscription
  useEffect(() => {
    const mqtt = MqttClient.getInstance();
    const topic = 'smarthome/fan/command';
    const handler = (_topic: string, payload: any) => {
      switch (payload.action) {
        case 'setPower':
          if (typeof payload.power === 'boolean') {
            setPower(payload.power);
            if (payload.power && speed === 0) setSpeed(1);
            if (!payload.power) setSpeed(0);
          }
          break;
        case 'setSpeed':
          if (typeof payload.speed === 'number') {
            changeSpeed(payload.speed);
            if (payload.speed > 0) setPower(true);
          }
          break;
        case 'setOscillation':
          if (typeof payload.enabled === 'boolean') { setOscillation(payload.enabled); setPower(true); }
          break;
      }
    };
    mqtt.subscribe(topic, handler);
    return () => mqtt.unsubscribe(topic, handler);
  }, [speed]);

  const formatTime = (seconds: number): string => {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  return (
    <div className="device-card">
      <div className="device-card-header">
        <h2>{t('fan.title')}</h2>
        <div className="device-status">
          <span className={`dot ${power ? 'on' : 'off'}`} />
          {power ? `${t('fan.speed')} ${speed} - ${t(SPEED_LABEL_KEYS[speed])}` : t('common.off')}
        </div>
      </div>
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
              style={{ '--spin-duration': SPIN_DURATIONS[speed] } as React.CSSProperties}
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
          {[0, 1, 2, 3].map((s) => (
            <button
              key={s}
              className={speed === s && (s > 0 || !power) ? 'active' : ''}
              onClick={() => changeSpeed(s)}
            >
              {t(SPEED_LABEL_KEYS[s])}
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
    </div>
  );
};

export default Fan;
