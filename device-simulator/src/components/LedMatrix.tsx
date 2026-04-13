import React, { useEffect, useRef, useState, useCallback } from 'react';
import { MqttClient } from '../mqtt/MqttClient';
import { useI18n } from '../i18n';

const GRID_SIZE = 16;
const TOTAL_PIXELS = GRID_SIZE * GRID_SIZE;

type LedMode = 'rainbow' | 'breathing' | 'chase' | 'sparkle' | 'fire' | 'ocean' | 'aurora' | 'solid';

function hslToHex(h: number, s: number, l: number): string {
  h = ((h % 360) + 360) % 360;
  s = Math.max(0, Math.min(100, s)) / 100;
  l = Math.max(0, Math.min(100, l)) / 100;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = l - c / 2;
  let r = 0, g = 0, b = 0;
  if (h < 60) { r = c; g = x; }
  else if (h < 120) { r = x; g = c; }
  else if (h < 180) { g = c; b = x; }
  else if (h < 240) { g = x; b = c; }
  else if (h < 300) { r = x; b = c; }
  else { r = c; b = x; }
  const toHex = (v: number) => Math.round((v + m) * 255).toString(16).padStart(2, '0');
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

function hexToRgb(hex: string): [number, number, number] {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return [r, g, b];
}

const LedMatrix: React.FC = () => {
  const [power, setPower] = useState(false);
  const [mode, setMode] = useState<LedMode>('rainbow');
  const [brightness, setBrightness] = useState(80);
  const [solidColor, setSolidColor] = useState('#ff0000');
  const [pixels, setPixels] = useState<string[]>(new Array(TOTAL_PIXELS).fill('#000000'));
  const frameRef = useRef(0);
  const tickRef = useRef(0);
  const { t } = useI18n();

  const generateFrame = useCallback(() => {
    if (!power) {
      return new Array(TOTAL_PIXELS).fill('#000000');
    }
    const tick = tickRef.current;
    const bMul = brightness / 100;
    const newPixels: string[] = new Array(TOTAL_PIXELS);

    switch (mode) {
      case 'rainbow': {
        for (let row = 0; row < GRID_SIZE; row++) {
          for (let col = 0; col < GRID_SIZE; col++) {
            const idx = row * GRID_SIZE + col;
            const hue = (tick * 4 + row * 20 + col * 20) % 360;
            const lightness = 50 + Math.sin((tick * 0.06) + row * 0.4 + col * 0.4) * 15;
            newPixels[idx] = hslToHex(hue, 100, lightness * bMul);
          }
        }
        break;
      }
      case 'breathing': {
        const breathPhase = (Math.sin(tick * 0.04) + 1) / 2;
        const pulseHue = (tick * 0.5) % 360;
        for (let i = 0; i < TOTAL_PIXELS; i++) {
          const row = Math.floor(i / GRID_SIZE);
          const col = i % GRID_SIZE;
          const dist = Math.sqrt(Math.pow(row - 7.5, 2) + Math.pow(col - 7.5, 2));
          const wave = Math.sin(tick * 0.04 - dist * 0.4) * 0.4 + 0.6;
          const light = wave * breathPhase * 60 * bMul;
          const hue = (pulseHue + dist * 8) % 360;
          newPixels[i] = hslToHex(hue, 100, Math.max(2, light));
        }
        break;
      }
      case 'chase': {
        for (let row = 0; row < GRID_SIZE; row++) {
          for (let col = 0; col < GRID_SIZE; col++) {
            const idx = row * GRID_SIZE + col;
            const pos = (row + col + tick * 0.4) % 16;
            const trail = Math.max(0, 1 - (pos % 6) / 3);
            const hue = (tick * 3 + (row + col) * 25) % 360;
            newPixels[idx] = hslToHex(hue, 100, trail * 60 * bMul);
          }
        }
        break;
      }
      case 'sparkle': {
        for (let i = 0; i < TOTAL_PIXELS; i++) {
          const sparkle = Math.random() > 0.88;
          if (sparkle) {
            const hue = Math.random() * 360;
            newPixels[i] = hslToHex(hue, 100, 65 * bMul);
          } else {
            const row = Math.floor(i / GRID_SIZE);
            const col = i % GRID_SIZE;
            const ambient = Math.sin(tick * 0.03 + row * 0.5 + col * 0.5) * 5 + 8;
            newPixels[i] = hslToHex((tick * 0.5 + row * 10) % 360, 60, ambient * bMul);
          }
        }
        break;
      }
      case 'fire': {
        for (let row = 0; row < GRID_SIZE; row++) {
          for (let col = 0; col < GRID_SIZE; col++) {
            const idx = row * GRID_SIZE + col;
            const heat = Math.max(0, (GRID_SIZE - row) / GRID_SIZE
              + Math.sin(tick * 0.1 + col * 0.8) * 0.35
              + Math.sin(tick * 0.15 + col * 1.3 + row * 0.5) * 0.25
              + (Math.random() * 0.18));
            const clampedHeat = Math.min(1, heat);
            const hue = clampedHeat < 0.5 ? clampedHeat * 30 : 15 + clampedHeat * 30;
            const light = clampedHeat * 58 * bMul;
            newPixels[idx] = hslToHex(hue, 100, Math.max(0, light));
          }
        }
        break;
      }
      case 'ocean': {
        for (let row = 0; row < GRID_SIZE; row++) {
          for (let col = 0; col < GRID_SIZE; col++) {
            const idx = row * GRID_SIZE + col;
            const wave1 = Math.sin(tick * 0.04 + col * 0.5 + row * 0.25) * 0.5 + 0.5;
            const wave2 = Math.sin(tick * 0.06 + col * 0.25 - row * 0.35) * 0.4 + 0.5;
            const combined = (wave1 + wave2) / 2;
            const hue = 180 + combined * 60;
            const light = 20 + combined * 45;
            newPixels[idx] = hslToHex(hue, 95, light * bMul);
          }
        }
        break;
      }
      case 'aurora': {
        for (let row = 0; row < GRID_SIZE; row++) {
          for (let col = 0; col < GRID_SIZE; col++) {
            const idx = row * GRID_SIZE + col;
            const n1 = Math.sin(tick * 0.025 + col * 0.35) * Math.cos(tick * 0.018 + row * 0.45);
            const n2 = Math.sin(tick * 0.03 + row * 0.25 + col * 0.15);
            const combined = (n1 + n2 + 2) / 4;
            const hue = 90 + combined * 200;
            const verticalFade = Math.pow(1 - row / GRID_SIZE, 0.5);
            const light = 10 + combined * 55 * verticalFade;
            newPixels[idx] = hslToHex(hue, 95, Math.max(3, light * bMul));
          }
        }
        break;
      }
      case 'solid': {
        const [r, g, b] = hexToRgb(solidColor);
        const color = `rgb(${Math.round(r * bMul)},${Math.round(g * bMul)},${Math.round(b * bMul)})`;
        newPixels.fill(color);
        break;
      }
    }
    return newPixels;
  }, [power, mode, brightness, solidColor]);

  useEffect(() => {
    let running = true;
    const animate = () => {
      if (!running) return;
      tickRef.current += 1;
      setPixels(generateFrame());
      frameRef.current = requestAnimationFrame(animate);
    };
    frameRef.current = requestAnimationFrame(animate);
    return () => {
      running = false;
      cancelAnimationFrame(frameRef.current);
    };
  }, [generateFrame]);

  // MQTT subscription
  useEffect(() => {
    const mqtt = MqttClient.getInstance();
    const topic = 'smarthome/led_matrix/command';
    const handler = (_topic: string, payload: any) => {
      switch (payload.action) {
        case 'setMode':
          if (payload.mode) { setMode(payload.mode as LedMode); setPower(true); }
          break;
        case 'setPower':
          if (typeof payload.power === 'boolean') setPower(payload.power);
          break;
        case 'setBrightness':
          if (typeof payload.brightness === 'number') setBrightness(payload.brightness);
          break;
        case 'setColor':
          if (payload.color) {
            setSolidColor(payload.color);
            setMode('solid');
          }
          break;
      }
    };
    mqtt.subscribe(topic, handler);
    return () => mqtt.unsubscribe(topic, handler);
  }, []);

  const modes: { key: LedMode; labelKey: string }[] = [
    { key: 'rainbow', labelKey: 'led.mode.rainbow' },
    { key: 'breathing', labelKey: 'led.mode.breathing' },
    { key: 'chase', labelKey: 'led.mode.chase' },
    { key: 'sparkle', labelKey: 'led.mode.sparkle' },
    { key: 'fire', labelKey: 'led.mode.fire' },
    { key: 'ocean', labelKey: 'led.mode.ocean' },
    { key: 'aurora', labelKey: 'led.mode.aurora' },
    { key: 'solid', labelKey: 'led.mode.solid' },
  ];

  return (
    <div className="device-card">
      <div className="device-card-header">
        <h2>{t('led.title')}</h2>
        <div className="device-status">
          <span className={`dot ${power ? 'on' : 'off'}`} />
          {power ? mode.charAt(0).toUpperCase() + mode.slice(1) : t('common.off')}
        </div>
      </div>
      <div className="led-panel">
        <div className="led-grid">
          {pixels.map((color, i) => (
            <div
              key={i}
              className="led-pixel"
              style={{
                backgroundColor: color,
                boxShadow: power && color !== '#000000' ? `0 0 4px ${color}, 0 0 8px ${color}88` : 'none',
              }}
            />
          ))}
        </div>
        <div className="led-controls">
          <button
            className={power ? 'active' : ''}
            onClick={() => setPower(!power)}
            style={power ? { background: '#1a3a1a', borderColor: '#22c55e', color: '#22c55e' } : {}}
          >
            {power ? t('common.on') : t('common.off')}
          </button>
          {modes.map((m) => (
            <button
              key={m.key}
              className={mode === m.key && power ? 'active' : ''}
              onClick={() => { setMode(m.key); setPower(true); }}
            >
              {t(m.labelKey)}
            </button>
          ))}
        </div>
        <div className="led-info">
          <span>
            {t('led.brightness')}
            <input
              type="range"
              min={0}
              max={100}
              value={brightness}
              onChange={(e) => setBrightness(Number(e.target.value))}
              style={{ width: 80, marginLeft: 4, accentColor: '#a78bfa' }}
            />
            {brightness}%
          </span>
        </div>
      </div>
    </div>
  );
};

export default LedMatrix;
