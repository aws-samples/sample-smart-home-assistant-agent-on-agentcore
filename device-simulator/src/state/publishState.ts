/**
 * Device state reporting: browser -> MQTT -> IoT rule -> DynamoDB.
 *
 * Until this existed the link was one-way. The simulator only ever SUBSCRIBED
 * to `.../command`; `MqttClient.publish` was defined and never called, there
 * were no IoT topic rules, and device state lived in React `useState` — so it
 * vanished on refresh and nothing in the cloud could read it. Any question of
 * the form "what is the temperature" or "is that light on" had nowhere to look.
 *
 * Reports are debounced because dragging a brightness slider fires a state
 * change per pixel of travel; without it a single drag would publish dozens of
 * messages and write each one to DynamoDB.
 */
import { MqttClient } from '../mqtt/MqttClient';

/** Slider drags settle well inside this; it stays under a human "did it save?" pause. */
const DEBOUNCE_MS = 400;

const timers = new Map<string, ReturnType<typeof setTimeout>>();
const pending = new Map<string, Record<string, unknown>>();

export function stateTopic(userSub: string, deviceId: string): string {
  return `smarthome/${userSub}/${deviceId}/state`;
}

/**
 * Report a device's current state. Repeated calls for the same device within
 * the debounce window collapse into one publish carrying the latest values.
 */
export function reportState(
  userSub: string,
  deviceId: string,
  state: Record<string, unknown>,
  options: { online?: boolean; immediate?: boolean } = {},
): void {
  if (!userSub || !deviceId) return;

  const key = `${userSub}/${deviceId}`;
  pending.set(key, { ...state });

  const flush = () => {
    timers.delete(key);
    const latest = pending.get(key);
    pending.delete(key);
    if (!latest) return;
    const mqtt = MqttClient.getInstance();
    void mqtt
      .publish(stateTopic(userSub, deviceId), {
        deviceId,
        state: latest,
        online: options.online !== false,
        reportedAt: new Date().toISOString(),
      })
      // A dropped state report must never break the UI — the device keeps
      // working locally, the cloud copy just goes stale until the next change.
      .catch((err) => console.warn('[state] publish failed', deviceId, err));
  };

  const existing = timers.get(key);
  if (existing) clearTimeout(existing);

  if (options.immediate) {
    flush();
    return;
  }
  timers.set(key, setTimeout(flush, DEBOUNCE_MS));
}

/**
 * Report a device as offline.
 *
 * Sent on page unload so "the simulator is closed" is distinguishable from
 * "nothing changed recently". This is best-effort — a hard tab kill sends
 * nothing, which is why the state table also carries a TTL.
 */
export function reportOffline(userSub: string, deviceId: string): void {
  if (!userSub || !deviceId) return;
  const mqtt = MqttClient.getInstance();
  void mqtt
    .publish(stateTopic(userSub, deviceId), {
      deviceId,
      online: false,
      reportedAt: new Date().toISOString(),
    })
    .catch(() => undefined);
}

/** Publish a batch of sensor readings as history seed/append points. */
export function reportSensorHistory(
  userSub: string,
  deviceId: string,
  points: Array<{ ts: number; metric: string; value: number }>,
): void {
  if (!userSub || !deviceId || points.length === 0) return;
  const mqtt = MqttClient.getInstance();
  void mqtt
    .publish(`smarthome/${userSub}/${deviceId}/history`, { deviceId, points })
    .catch((err) => console.warn('[history] publish failed', deviceId, err));
}
