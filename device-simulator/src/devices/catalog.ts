/**
 * Frontend view of the shared device catalog.
 *
 * The JSON is imported directly (webpack resolves it via the `json` module
 * type), so the simulator renders whatever the catalog declares instead of
 * hardcoding a component list. Adding a device to the catalog adds a card here
 * and a validated command path in the Lambdas at the same time — which is the
 * point of having one definition.
 */
import catalogJson from '../../../shared/device-catalog.json';

export type CapabilityKind =
  | 'boolean'
  | 'integer'
  | 'enum'
  | 'color'
  | 'segments'
  | 'readonly';

export interface Capability {
  type: CapabilityKind;
  min?: number;
  max?: number;
  values?: string[];
  count?: number;
  unit?: string;
}

export interface ActionSpec {
  writes: string;
  required?: string[];
  optional?: string[];
  /** Wire parameter -> capability, for names that differ (`enabled` -> `oscillation`). */
  params?: Record<string, string>;
  /** Side effects the device performs itself, e.g. setSpeed implies power=true. */
  implies?: Record<string, unknown>;
}

export interface DeviceDef {
  deviceId: string;
  deviceType: string;
  displayName: Record<string, string>;
  room: string;
  connectivity: string;
  capabilities: Record<string, Capability>;
  actions: Record<string, ActionSpec>;
  powerOn?: Record<string, unknown>;
  powerOff?: Record<string, unknown>;
}

interface Catalog {
  rooms: Record<string, Record<string, string>>;
  devices: DeviceDef[];
}

const catalog = catalogJson as unknown as Catalog;

export const DEVICES: DeviceDef[] = catalog.devices;
export const ROOMS = catalog.rooms;

export function deviceById(id: string): DeviceDef | undefined {
  return DEVICES.find((d) => d.deviceId === id);
}

export function displayName(device: DeviceDef, language: string): string {
  return device.displayName[language] || device.displayName.en || device.deviceId;
}

export function roomName(device: DeviceDef, language: string): string {
  const room = ROOMS[device.room] || {};
  return room[language] || room.en || device.room;
}

export function isReadOnly(device: DeviceDef): boolean {
  return Object.keys(device.actions || {}).length === 0;
}

/** Clamp to a capability's declared range, mirroring the Lambda's behaviour. */
export function clampToCapability(cap: Capability | undefined, value: number): number {
  if (!cap) return value;
  if (cap.min !== undefined && value < cap.min) return cap.min;
  if (cap.max !== undefined && value > cap.max) return cap.max;
  return value;
}

/**
 * Initial state for a device, derived from its capabilities.
 *
 * Devices start off rather than at a random state so a fresh session reads the
 * same in the UI and in DynamoDB.
 */
export function initialState(device: DeviceDef): Record<string, unknown> {
  const state: Record<string, unknown> = {};
  for (const [name, cap] of Object.entries(device.capabilities)) {
    switch (cap.type) {
      case 'boolean':
        state[name] = false;
        break;
      case 'integer': {
        const min = cap.min ?? 0;
        const max = cap.max ?? 100;
        // Most integers start at their floor (a fan starts off, an oven cold).
        // Two do not: brightness at 0 makes a light that is "on" look broken,
        // and colour temperature at its floor pins a white-tunable lamp to the
        // extreme warm end when neutral is the sane resting point.
        if (name === 'brightness') state[name] = Math.round(min + (max - min) * 0.8);
        else if (name === 'color_temp') state[name] = Math.round((min + max) / 2);
        else state[name] = min;
        break;
      }
      case 'enum':
        state[name] = cap.values?.[0];
        break;
      case 'color':
        state[name] = '#ffffff';
        break;
      case 'segments':
        state[name] = [];
        break;
      case 'readonly':
        // Sensor readings are seeded by the sensor component's own simulation.
        break;
      default:
        break;
    }
  }
  return state;
}
