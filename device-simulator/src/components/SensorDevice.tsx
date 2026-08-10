import React, { useEffect, useRef, useState } from 'react';
import { DeviceCard } from './DeviceCard';
import { MqttClient } from '../mqtt/MqttClient';
import { getMultiplier, nowSeconds, subscribe as subscribeClock } from '../devices/virtualClock';
import { DeviceDef } from '../devices/catalog';
import { reportSensorHistory, reportState } from '../state/publishState';
import { useI18n } from '../i18n';

interface Props {
  device: DeviceDef;
  userSub: string;
}

/** 5-minute samples, so 24h is 288 points — the interval the backfill assumes. */
const SAMPLE_SECONDS = 300;
const BACKFILL_HOURS = 24;
/** Live sampling cadence. Fast enough to look alive, slow enough not to spam. */
const LIVE_INTERVAL_MS = 30_000;

/**
 * Environment sensor: current readings plus a 24h trend.
 *
 * Read-only — it declares no actions, so the control Lambda refuses commands
 * against it and tells the agent what it can report instead. That is what lets
 * "turn on the thermometer" be answered by querying rather than failing.
 *
 * Once MQTT is up it backfills a day of history in ONE message (288 points x 4
 * metrics) rather than publishing each point: without seeded history, "the
 * temperature over the last 24 hours" has nothing to read until the simulator has
 * been left open for a day.
 */
export function SensorDevice({ device, userSub }: Props) {
  const { t, language } = useI18n();
  const metrics = Object.entries(device.capabilities)
    .filter(([, cap]) => cap.type === 'readonly');

  const [readings, setReadings] = useState<Record<string, number>>({});
  const [history, setHistory] = useState<Record<string, number[]>>({});
  const seeded = useRef(false);
  // Sampling cadence follows the virtual clock's speed, so this has to re-run the
  // live-sampling effect when the multiplier changes.
  const [multiplier, setMultiplierState] = useState(() => getMultiplier());
  useEffect(() => subscribeClock((c) => setMultiplierState(c.multiplier)), []);

  // The backfill is a single large publish, so it has to wait for the MQTT
  // handshake — sending it on mount drops the whole day of history into a closed
  // socket, and there is no retry because it only runs once.
  const [connected, setConnected] = useState(() => MqttClient.getInstance().isConnected());
  useEffect(() => MqttClient.getInstance().onConnectionChange(setConnected), []);

  useEffect(() => {
    if (!userSub || !connected || seeded.current) return;
    seeded.current = true;

    const now = nowSeconds();
    const count = (BACKFILL_HOURS * 3600) / SAMPLE_SECONDS;
    const points: Array<{ ts: number; metric: string; value: number }> = [];
    const series: Record<string, number[]> = {};
    const current: Record<string, number> = {};

    for (const [metric, cap] of metrics) {
      const min = cap.min ?? 0;
      const max = cap.max ?? 100;
      // Centre the walk in the comfortable part of the range rather than the
      // midpoint of the sensor's full span — a thermometer that reads -10..50
      // should sit near 22, not 20.
      const centre = metric === 'temperature' ? 22
        : metric === 'humidity' ? 48
        : metric === 'pm25' ? 15
        : metric === 'co2' ? 620
        : (min + max) / 2;
      const swing = Math.min((max - min) * 0.08, Math.max(1, centre * 0.15));

      const values: number[] = [];
      let value = centre;
      for (let i = 0; i < count; i++) {
        // Daily cycle plus a small random walk, pulled back toward centre so it
        // does not drift out of range over a long session.
        const hourOfDay = ((now - (count - i) * SAMPLE_SECONDS) / 3600) % 24;
        const diurnal = Math.sin((hourOfDay / 24) * Math.PI * 2 - Math.PI / 2) * swing;
        value += (centre - value) * 0.05 + (Math.random() - 0.5) * swing * 0.3;
        const sample = Math.max(min, Math.min(max, Math.round((value + diurnal) * 10) / 10));
        values.push(sample);
        points.push({ ts: now - (count - i) * SAMPLE_SECONDS, metric, value: sample });
      }
      series[metric] = values;
      current[metric] = values[values.length - 1];
    }

    setHistory(series);
    setReadings(current);
    reportSensorHistory(userSub, device.deviceId, points);
    // A sensor's "state" is its latest readings, so the agent can answer "what
    // is the temperature now" from the state table without a history query.
    reportState(userSub, device.deviceId, current, { immediate: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userSub, connected, device.deviceId]);

  // Live sampling.
  useEffect(() => {
    if (!userSub || !connected) return;
    const timer = setInterval(() => {
      const now = nowSeconds();
      const points: Array<{ ts: number; metric: string; value: number }> = [];
      setReadings((prev) => {
        const next: Record<string, number> = {};
        for (const [metric, cap] of metrics) {
          const min = cap.min ?? 0;
          const max = cap.max ?? 100;
          const base = prev[metric] ?? (min + max) / 2;
          const step = Math.max(0.1, (max - min) * 0.004);
          const value = Math.max(min, Math.min(max,
            Math.round((base + (Math.random() - 0.5) * step * 2) * 10) / 10));
          next[metric] = value;
          points.push({ ts: now, metric, value });
        }
        setHistory((h) => {
          const merged: Record<string, number[]> = {};
          for (const [metric] of metrics) {
            const series = [...(h[metric] ?? []), next[metric]];
            merged[metric] = series.slice(-((BACKFILL_HOURS * 3600) / SAMPLE_SECONDS));
          }
          return merged;
        });
        reportSensorHistory(userSub, device.deviceId, points);
        reportState(userSub, device.deviceId, next);
        return next;
      });
      // Sample on VIRTUAL time. At 60x the timestamps advance five virtual hours
      // between real-time ticks, so a fixed 30s interval would leave the history a
      // handful of points scattered across a simulated day — a threshold trigger
      // reading that series would see almost nothing. Re-armed each tick rather
      // than using a fixed interval so a multiplier change takes effect at once.
    }, Math.max(500, LIVE_INTERVAL_MS / getMultiplier()));
    return () => clearInterval(timer);
    // Re-arm when the multiplier changes: `clock` is state fed by the clock's own
    // subscription, so this effect re-runs and the interval is recomputed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userSub, connected, device.deviceId, multiplier]);

  const label = (metric: string) => {
    const key = `metric.${metric}`;
    return t(key) === key ? metric : t(key);
  };

  return (
    <DeviceCard
      device={device}
      status={t('sensor.readOnly')}
      active={Object.keys(readings).length > 0}
    >
      <div className="sensor-readings">
        {metrics.map(([metric, cap]) => (
          <div className="sensor-reading" key={metric}>
            <div className="sensor-label">{label(metric)}</div>
            <div className="sensor-value">
              {readings[metric] !== undefined ? readings[metric] : '--'}
              <span className="sensor-unit">{cap.unit ?? ''}</span>
            </div>
            <Sparkline values={history[metric] ?? []} />
          </div>
        ))}
      </div>
      <div className="sensor-footnote">
        {t('sensor.historyNote').replace('{hours}', String(BACKFILL_HOURS))}
      </div>
    </DeviceCard>
  );
}

/**
 * Inline 24h trend. Deliberately unlabelled and unscaled — it is a shape, not a
 * chart; the numeric reading above it carries the value.
 */
function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) return <div className="sensor-spark" />;
  // Thin to a drawable number of points; 288 path commands per sensor per frame
  // is wasted work at this size.
  const step = Math.max(1, Math.floor(values.length / 48));
  const points = values.filter((_, i) => i % step === 0);
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const path = points
    .map((v, i) => {
      const x = (i / (points.length - 1)) * 100;
      const y = 100 - ((v - min) / span) * 100;
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  return (
    <svg className="sensor-spark" viewBox="0 0 100 100" preserveAspectRatio="none">
      <path d={path} fill="none" stroke="currentColor" strokeWidth={2} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
