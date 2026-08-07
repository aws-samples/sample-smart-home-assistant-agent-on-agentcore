import { useCallback, useEffect, useRef, useState } from 'react';
import { MqttClient } from '../mqtt/MqttClient';
import { DeviceDef, Capability, clampToCapability, initialState } from '../devices/catalog';
import { reportState, reportOffline } from './publishState';

type State = Record<string, unknown>;

/**
 * One device's state, wired to MQTT in both directions.
 *
 * Each of the four original components hand-rolled its own useState set, its own
 * `smarthome/{sub}/{type}/command` subscription and its own switch over actions.
 * That was ~40 lines duplicated per device with the action names spelled out by
 * hand — the drift that let the UI accept led mode 'solid' while the control
 * Lambda rejected it. Here the catalog's `actions` map drives the update, so a
 * device's wire protocol is declared once and honoured on both sides.
 *
 * The subscription callback intentionally does NOT re-report to `/state`
 * directly; it updates local state, and the effect below reports whatever the
 * state currently is. That way an agent command and a button click take the same
 * path out.
 */
export function useDeviceState(device: DeviceDef, userSub: string) {
  const [state, setState] = useState<State>(() => initialState(device));

  // Reporting reads the latest state without making the effect depend on every
  // field, which would resubscribe on every change.
  const latest = useRef(state);
  latest.current = state;

  /** Apply one field, clamped to its declared range. */
  const set = useCallback((field: string, value: unknown) => {
    setState((prev) => {
      const cap: Capability | undefined = device.capabilities[field];
      const next = typeof value === 'number' ? clampToCapability(cap, value) : value;
      if (prev[field] === next) return prev;
      return { ...prev, [field]: next };
    });
  }, [device]);

  const merge = useCallback((patch: State) => {
    setState((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const [field, value] of Object.entries(patch)) {
        const cap: Capability | undefined = device.capabilities[field];
        const v = typeof value === 'number' ? clampToCapability(cap, value) : value;
        if (next[field] !== v) {
          next[field] = v;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [device]);

  // Inbound commands from the agent.
  useEffect(() => {
    if (!userSub) return;
    const mqtt = MqttClient.getInstance();
    const topic = `smarthome/${userSub}/${device.deviceId}/command`;

    const handler = (_topic: string, payload: any) => {
      const spec = device.actions[payload?.action];
      if (!spec) {
        console.warn('[device] unknown action', device.deviceId, payload?.action);
        return;
      }
      const patch: State = {};
      const params = [...(spec.required || []), ...(spec.optional || [])];
      for (const param of params) {
        if (payload[param] === undefined) continue;
        // Wire parameter -> capability, for the names that differ
        // (`enabled` -> `oscillation`, `colors` -> `segments`).
        const field = spec.params?.[param] || param;
        patch[field] = payload[param];
      }
      // An action with no parameters still means something: `stop` clears the
      // boolean it writes.
      if (params.length === 0 && spec.writes) {
        patch[spec.writes] = false;
      }
      // Side effects the device performs itself — "set the fan to 5" runs the
      // fan. Declared in the catalog so a click and an MQTT command agree, and
      // so the agent does not have to know to send a second power command.
      // Applied first so an explicit parameter still wins.
      merge({ ...(spec.implies || {}), ...patch });
    };

    mqtt.subscribe(topic, handler);
    return () => mqtt.unsubscribe(topic, handler);
  }, [device, userSub, merge]);

  // Outbound state reports. Debounced inside reportState, so a slider drag
  // collapses to one publish.
  //
  // Gated on the connection: components mount and compute their initial state
  // well before the MQTT handshake finishes (~3s here), so an ungated report
  // publishes into a closed socket and is dropped with a console warning. That
  // left the cloud with no state at all for any device the user never touched.
  // Reporting again on connect is what makes a freshly-opened simulator
  // readable.
  const [connected, setConnected] = useState(() => MqttClient.getInstance().isConnected());
  useEffect(() => MqttClient.getInstance().onConnectionChange(setConnected), []);

  useEffect(() => {
    if (!userSub || !connected) return;
    reportState(userSub, device.deviceId, state);
  }, [state, userSub, device.deviceId, connected]);

  // Say goodbye on unload so "simulator closed" is distinguishable from
  // "nothing changed lately". Best-effort: a hard tab kill sends nothing, which
  // is why the state table also has a TTL.
  useEffect(() => {
    if (!userSub) return;
    const onUnload = () => reportOffline(userSub, device.deviceId);
    window.addEventListener('pagehide', onUnload);
    return () => window.removeEventListener('pagehide', onUnload);
  }, [userSub, device.deviceId]);

  return { state, set, merge };
}
