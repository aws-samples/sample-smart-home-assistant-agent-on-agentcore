import React, { useEffect, useState } from 'react';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import SpaceBetween from '@cloudscape-design/components/space-between';
import {
  ClockState,
  MULTIPLIERS,
  formatDateTime,
  formatTime,
  nowMs,
  reset,
  setMultiplier,
  skipMinutes,
  subscribe,
} from '../devices/virtualClock';
import { useI18n } from '../i18n';

/**
 * The simulator's clock, with a speed control.
 *
 * Shown because scheduled scenes are keyed on a wall-clock time in UTC, and
 * "every day at 23:00" is abstract until there is a clock on screen reading the
 * same zone the schedule uses. Local time is deliberately not displayed: it would
 * disagree with the stored trigger for most of the world, and a demo where the
 * clock says 08:30 and the scene says 23:00 invites exactly the wrong conclusion.
 *
 * The speed control accelerates the simulator's own time — the sensor curve a
 * threshold trigger reads, and this display. It does NOT move an EventBridge
 * schedule, which fires on real time inside AWS; the note in the panel says so,
 * because a demo that implies otherwise is worse than one that explains the limit.
 */
export const VirtualClock: React.FC = () => {
  const { t } = useI18n();
  const [clock, setClock] = useState<ClockState>(
    () => ({ nowMs: nowMs(), multiplier: 1, simulated: false }));

  useEffect(() => subscribe(setClock), []);

  // Repaint four times a second. At 3600x a second of real time is an hour of
  // virtual time, so a 1s tick would visibly jump; this keeps the digits moving
  // without a render loop.
  useEffect(() => {
    const timer = setInterval(
      () => setClock((prev) => ({ ...prev, nowMs: nowMs() })), 250);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="sim-clock">
      <SpaceBetween size="xs" direction="horizontal" alignItems="center">
        <Box variant="h3" padding="n">
          <span className="sim-clock-time">{formatTime(clock.nowMs)}</span>
          <span className="sim-clock-zone"> UTC</span>
        </Box>
        {clock.simulated && (
          <Badge color="blue">{`${clock.multiplier}x`}</Badge>
        )}
      </SpaceBetween>

      <Box variant="small" color="text-body-secondary">
        {formatDateTime(clock.nowMs)}
      </Box>

      <SpaceBetween size="xxs" direction="horizontal">
        {MULTIPLIERS.map((m) => (
          <Button
            key={m}
            variant={clock.multiplier === m ? 'primary' : 'normal'}
            onClick={() => setMultiplier(m)}
          >
            {m === 1 ? t('clock.realTime') : `${m}x`}
          </Button>
        ))}
      </SpaceBetween>

      <SpaceBetween size="xxs" direction="horizontal">
        <Button iconName="add-plus" onClick={() => skipMinutes(60)}>
          {t('clock.skipHour')}
        </Button>
        <Button onClick={() => skipMinutes(6 * 60)}>{t('clock.skip6Hours')}</Button>
        {clock.simulated && (
          <Button iconName="undo" onClick={reset}>{t('clock.reset')}</Button>
        )}
      </SpaceBetween>

      <Box variant="small" color="text-status-inactive">
        {t('clock.note')}
      </Box>
    </div>
  );
};

export default VirtualClock;
