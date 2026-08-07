import React from 'react';
import { DeviceDef, displayName, roomName } from '../devices/catalog';
import { useI18n } from '../i18n';

interface Props {
  device: DeviceDef;
  /** Short status line, e.g. "Speed 3" or "OFF". */
  status: React.ReactNode;
  /** True lights the status dot. Read-only devices pass their reachability. */
  active: boolean;
  children: React.ReactNode;
}

/**
 * Shared shell for a device card: title, room, status dot.
 *
 * The four original components each rebuilt this header inline. With eight
 * devices in the catalog and more expected, one shell keeps them consistent and
 * makes the room visible — which matters now that two devices can be the same
 * type and are told apart by room ("living room light" vs "bedroom light").
 */
export function DeviceCard({ device, status, active, children }: Props) {
  const { language } = useI18n();
  return (
    <div className="device-card">
      <div className="device-card-header">
        <div>
          <h2>{displayName(device, language)}</h2>
          <div className="device-room">{roomName(device, language)}</div>
        </div>
        <div className="device-status">
          <span className={`dot ${active ? 'on' : 'off'}`} />
          {status}
        </div>
      </div>
      {children}
    </div>
  );
}
