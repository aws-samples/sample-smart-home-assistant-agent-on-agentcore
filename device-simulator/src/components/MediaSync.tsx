import React, { useCallback, useEffect, useRef, useState } from 'react';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { MqttClient } from '../mqtt/MqttClient';
import { DeviceDef, displayName } from '../devices/catalog';
import {
  BeatState,
  SCENE_NAMES,
  SceneName,
  beatAt,
  musicColors,
  paintFrame,
} from '../devices/mediaSource';
import { reportState } from '../state/publishState';
import { useI18n } from '../i18n';

/**
 * The screen and the speaker that a scene syncs the lights to.
 *
 * These are the props §4.6 of the multi-agent spec asked for, and they exist
 * because the scene agent now has something to point at: "movie night" and "music
 * feast" were previously scenes that set a colour and called it ambience. Here the
 * TV backlight's four segments actually follow the four edges of a moving picture,
 * and in music mode they pulse on the beat.
 *
 * The bluetooth state machine is idle -> pairing -> connected, and the music sync
 * only drives the lights when it reaches `connected`. That sequence is the
 * interesting part to demonstrate: a scene that turns on music has to wait for a
 * speaker to connect, which is the kind of real-world latency an automation has
 * to tolerate. Pairing deliberately takes a couple of seconds rather than being
 * instant.
 *
 * `sync_mode` on the TV backlight is the device-side switch, and it was already in
 * the catalog and already validated by the control Lambda — the agent can set it
 * over MQTT today. This panel is what makes it visible.
 */

type SyncMode = 'off' | 'video' | 'music';
type BluetoothState = 'idle' | 'pairing' | 'connected';

const CANVAS_W = 320;
const CANVAS_H = 180;
const PAIRING_MS = 2200;

/**
 * How often the sync pushes colours to the cloud.
 *
 * Must be LONGER than publishState's 400ms debounce, not shorter. Each report
 * resets that debounce timer, so a 160ms cadence re-armed the timer before it ever
 * fired and nothing was published at all — the on-screen sync looked perfect while
 * the cloud kept the light's last manual state. Starvation, not a dropped message,
 * so nothing errored and no MQTT log line was missing.
 *
 * 600ms is also about the fastest a real ambient-light system pushes; the point is
 * the colour follows the picture, not that it follows every frame.
 */
const PUBLISH_INTERVAL_MS = 600;

interface Props {
  /** The TV backlight — the device whose segments follow the screen. */
  device: DeviceDef;
  userSub: string;
}

export const MediaSync: React.FC<Props> = ({ device, userSub }) => {
  const { t, language } = useI18n();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const tickRef = useRef(0);
  const frameRef = useRef(0);
  const startedAt = useRef(Date.now());

  const [scene, setScene] = useState<SceneName>('sunset');
  const [mode, setMode] = useState<SyncMode>('off');
  const [bluetooth, setBluetooth] = useState<BluetoothState>('idle');
  const [bpm, setBpm] = useState(112);
  const [edges, setEdges] = useState<string[]>(['#000000', '#000000', '#000000', '#000000']);
  const [beat, setBeat] = useState<BeatState>({ energy: 0, beat: 0, bpm: 112 });

  // The loop reads these from a ref so changing the scene does not restart it.
  const live = useRef({ mode, scene, bpm, bluetooth });
  live.current = { mode, scene, bpm, bluetooth };

  /** Push the current sync colours to the backlight, as the device reporting. */
  const publish = useCallback((segments: string[], syncMode: SyncMode) => {
    if (!userSub) return;
    reportState(userSub, device.deviceId, {
      power: syncMode !== 'off',
      sync_mode: syncMode,
      segments,
      // `effect: solid` because the segments ARE the frame — an effect motion on
      // top would fight the picture it is supposed to be following.
      effect: 'solid',
      // The speaker link, read from the ref so `publish` does not have to be
      // rebuilt (and the render loop restarted) every time pairing advances.
      //
      // Reported because music sync depends on it and nothing else can see it.
      // It lived only in this component's state, so an agent asked to make the
      // lights follow the music had no way to tell "paired in a moment" from
      // "never paired" — and the honest answer to the second is to tell the user
      // to reconnect, not to claim success. It is a readonly capability: the
      // catalog offers no action that writes it, so a command cannot forge it.
      bluetooth: live.current.bluetooth,
    });
  }, [device.deviceId, userSub]);

  // The device's own command topic: a scene sets sync_mode over MQTT, and this
  // panel has to follow it. Without this the agent could set the mode and the
  // screen would sit there, which is the sort of half-working that reads as a
  // broken feature.
  useEffect(() => {
    if (!userSub) return;
    const topic = `smarthome/${userSub}/${device.deviceId}/command`;
    // MessageCallback is (topic, payload) — the payload is the SECOND argument.
    // Reading the first gave this handler the topic string, so `sync_mode` was
    // always undefined and an agent's setSyncMode silently did nothing while the
    // panel's own buttons worked. `useDeviceState` got the same message correctly,
    // which is why the device state updated and only the screen ignored it.
    const handler = (_topic: string, payload: any) => {
      const next = payload?.sync_mode ?? payload?.command?.sync_mode;
      if (next === 'video' || next === 'music' || next === 'off') {
        setMode(next);
        if (next === 'music' && live.current.bluetooth === 'idle') {
          // A scene that asks for music implies wanting the speaker connected.
          setBluetooth('pairing');
        }
      }
    };
    const mqtt = MqttClient.getInstance();
    mqtt.subscribe(topic, handler);
    return () => mqtt.unsubscribe(topic, handler);
  }, [userSub, device.deviceId]);

  // Pairing settles after a beat, the way a real speaker does.
  useEffect(() => {
    if (bluetooth !== 'pairing') return;
    const timer = setTimeout(() => setBluetooth('connected'), PAIRING_MS);
    return () => clearTimeout(timer);
  }, [bluetooth]);

  // Report every pairing transition, not just the settled state.
  //
  // The render loop only publishes while music sync is actually driving colours,
  // which is to say only once bluetooth is `connected`. So an agent that set music
  // mode and then polled would never observe `pairing` — it would read whatever was
  // reported before, decide the link was idle, and tell the user to reconnect a
  // speaker that was two seconds from being ready. Publishing the transition is
  // what makes "wait for it" a thing the agent can actually do.
  //
  // The WHOLE state goes in the message, not just the changed field. The IoT topic
  // rule writes `state` as one attribute, so a partial report replaces the item
  // rather than merging into it — publishing `{bluetooth}` alone would erase
  // power, sync_mode and segments, and `query_device_state` would then report the
  // backlight as off in the middle of driving it.
  useEffect(() => {
    if (!userSub) return;
    reportState(userSub, device.deviceId, {
      power: mode !== 'off',
      sync_mode: mode,
      segments: edges,
      effect: 'solid',
      bluetooth,
    });
    // Deliberately keyed on `bluetooth` alone: this effect exists to report the
    // pairing transition, and the render loop already publishes colour changes at
    // its own throttled rate. Adding `edges` here would fire it 60 times a second.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bluetooth, userSub, device.deviceId]);

  // The render loop: paint a frame, read its edges, drive the light.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) return;

    let running = true;
    let lastPublish = 0;

    const animate = () => {
      if (!running) return;
      const s = live.current;
      tickRef.current += 1;

      if (s.mode === 'video') {
        const frame = paintFrame(ctx, s.scene, tickRef.current, CANVAS_W, CANVAS_H);
        setEdges(frame.edges);
        // Throttled. The animation is smooth on screen at 60fps, but a state
        // report per frame would be 60 MQTT publishes a second per device — the
        // light cannot resolve that and the broker should not have to.
        const now = Date.now();
        if (now - lastPublish > PUBLISH_INTERVAL_MS) {
          lastPublish = now;
          publish(frame.edges, 'video');
        }
      } else if (s.mode === 'music') {
        const b = beatAt(Date.now() - startedAt.current, s.bpm);
        setBeat(b);
        if (s.bluetooth === 'connected') {
          const count = device.capabilities.segments?.count ?? 4;
          const colors = musicColors(s.scene, b, count);
          setEdges(colors.slice(0, 4));
          const now = Date.now();
          if (now - lastPublish > PUBLISH_INTERVAL_MS) {
            lastPublish = now;
            publish(colors, 'music');
          }
        }
        // Keep painting so the screen is not blank while music plays.
        paintFrame(ctx, s.scene, tickRef.current, CANVAS_W, CANVAS_H);
      } else {
        ctx.fillStyle = '#0b0b0b';
        ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);
      }
      frameRef.current = requestAnimationFrame(animate);
    };

    frameRef.current = requestAnimationFrame(animate);
    return () => {
      running = false;
      cancelAnimationFrame(frameRef.current);
    };
  }, [publish, device.capabilities.segments]);

  const setModeAndPublish = (next: SyncMode) => {
    setMode(next);
    if (next === 'music' && bluetooth === 'idle') setBluetooth('pairing');
    if (next === 'off') publish([], 'off');
  };

  const btIndicator = bluetooth === 'connected'
    ? <StatusIndicator type="success">{t('media.btConnected')}</StatusIndicator>
    : bluetooth === 'pairing'
      ? <StatusIndicator type="in-progress">{t('media.btPairing')}</StatusIndicator>
      : <StatusIndicator type="stopped">{t('media.btIdle')}</StatusIndicator>;

  return (
    <div className="sim-media">
      <SpaceBetween size="s">
        <Box variant="h3" padding="n">{t('media.title')}</Box>
        <Box variant="small" color="text-body-secondary">{t('media.desc')}</Box>

        <div className="sim-media-screen">
          <canvas ref={canvasRef} width={CANVAS_W} height={CANVAS_H} />
          {/* The four edge colours, in the order the backlight's segments run.
              Shown next to the picture so the sync is checkable by eye rather
              than taken on trust. */}
          <div className="sim-media-edges">
            {edges.slice(0, 4).map((c, i) => (
              <span key={i} className="sim-media-edge" style={{ background: c }}
                    title={c} />
            ))}
          </div>
        </div>

        <SpaceBetween size="xxs" direction="horizontal">
          <Button variant={mode === 'off' ? 'primary' : 'normal'}
                  onClick={() => setModeAndPublish('off')}>
            {t('media.off')}
          </Button>
          <Button variant={mode === 'video' ? 'primary' : 'normal'}
                  onClick={() => setModeAndPublish('video')}>
            {t('media.video')}
          </Button>
          <Button variant={mode === 'music' ? 'primary' : 'normal'}
                  onClick={() => setModeAndPublish('music')}>
            {t('media.music')}
          </Button>
        </SpaceBetween>

        <div style={{ maxWidth: 220 }}>
          <Select
            selectedOption={{ value: scene, label: t(`media.scene.${scene}`) }}
            onChange={({ detail }) => setScene(detail.selectedOption.value as SceneName)}
            options={SCENE_NAMES.map((s) => ({ value: s, label: t(`media.scene.${s}`) }))}
          />
        </div>

        {mode === 'music' && (
          <SpaceBetween size="xs" direction="horizontal" alignItems="center">
            {btIndicator}
            {bluetooth === 'idle' && (
              <Button onClick={() => setBluetooth('pairing')}>
                {t('media.pair')}
              </Button>
            )}
            <Badge color={beat.beat === 0 ? 'red' : 'grey'}>
              {`${bpm} BPM`}
            </Badge>
            <div className="sim-beat-meter">
              <div className="sim-beat-fill"
                   style={{ width: `${Math.round(beat.energy * 100)}%` }} />
            </div>
            <Button iconName="angle-left" ariaLabel="slower"
                    onClick={() => setBpm((b) => Math.max(60, b - 8))} />
            <Button iconName="angle-right" ariaLabel="faster"
                    onClick={() => setBpm((b) => Math.min(180, b + 8))} />
          </SpaceBetween>
        )}

        <Box variant="small" color="text-status-inactive">
          {/* The catalog's localised name rather than the English field: the note
              is translated, and splicing an English device name into a Chinese
              sentence is the sort of half-localisation that looks like a bug. */}
          {t('media.note').replace('{device}', displayName(device, language))}
        </Box>
      </SpaceBetween>
    </div>
  );
};

export default MediaSync;
