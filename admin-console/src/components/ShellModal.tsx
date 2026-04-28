/**
 * Remote Shell modal — opens from a Sessions-tab row and runs a single
 * shell command inside the targeted AgentCore Runtime via
 * InvokeAgentRuntimeCommand, streaming stdout/stderr back live.
 *
 * Self-contained state: command, timeout, runtime selector, output, history,
 * abort controller. Resets on close.
 */
import React, { useMemo, useState, useCallback } from 'react';

import AnsiOutput, { chunksToPlainText } from './AnsiOutput';
import { useI18n } from '../i18n';
import { getConfig } from '../config';
import { getAwsCredentials } from '../auth/awsCredentials';
import { runCommand, ShellChunk } from '../api/agentcoreCommand';

const MAX_COMMAND_BYTES = 64 * 1024;
const MAX_TIMEOUT_SECONDS = 3600;
const DEFAULT_TIMEOUT_SECONDS = 300;
const MIN_SESSION_ID_LEN = 33;
const HISTORY_MAX = 20;

export interface ShellTarget {
  userId: string;
  sessionId: string;
  kind: 'text' | 'voice';
}

interface Props {
  target: ShellTarget;
  onClose: () => void;
}

function byteLen(s: string): number {
  return new TextEncoder().encode(s).length;
}

type ValidationCode = 'empty' | 'tooLarge' | 'timeout' | 'sessionIdTooShort';

function validate(
  command: string,
  timeout: number,
  sessionId: string,
): ValidationCode | null {
  if (!command || command.length < 1) return 'empty';
  if (byteLen(command) > MAX_COMMAND_BYTES) return 'tooLarge';
  if (!Number.isFinite(timeout) || timeout < 1 || timeout > MAX_TIMEOUT_SECONDS) return 'timeout';
  if (!sessionId || sessionId.length < MIN_SESSION_ID_LEN) return 'sessionIdTooShort';
  return null;
}

const ShellModal: React.FC<Props> = ({ target, onClose }) => {
  const { t } = useI18n();
  const config = getConfig();

  const [runtime, setRuntime] = useState<'text' | 'voice'>(target.kind);
  const [command, setCommand] = useState<string>('');
  const [timeout, setTimeoutValue] = useState<number>(DEFAULT_TIMEOUT_SECONDS);
  const [chunks, setChunks] = useState<ShellChunk[]>([]);
  const [history, setHistory] = useState<string[]>([]);
  const [running, setRunning] = useState<boolean>(false);
  const [exit, setExit] = useState<{ code: number; status: string; ms: number } | null>(null);
  const [abortCtrl, setAbortCtrl] = useState<AbortController | null>(null);
  const [error, setError] = useState<string | null>(null);

  const runtimeArn = runtime === 'voice' ? config.voiceAgentRuntimeArn : config.agentRuntimeArn;

  const bytes = byteLen(command);
  const validationErr = useMemo(
    () => validate(command, timeout, target.sessionId),
    [command, timeout, target.sessionId],
  );
  const canRun = !running && validationErr === null && !!runtimeArn;

  const onRun = useCallback(async () => {
    if (!canRun) return;
    setError(null);
    setExit(null);
    setChunks([]);
    setRunning(true);

    const ac = new AbortController();
    setAbortCtrl(ac);

    setHistory((prev) => {
      const next = [command, ...prev.filter((c) => c !== command)];
      return next.slice(0, HISTORY_MAX);
    });

    const started = Date.now();
    try {
      const credentials = await getAwsCredentials();
      for await (const chunk of runCommand({
        agentRuntimeArn: runtimeArn,
        runtimeSessionId: target.sessionId,
        command,
        timeoutSeconds: timeout,
        credentials,
        region: config.region,
        signal: ac.signal,
      })) {
        if (chunk.kind === 'exit') {
          setExit({ code: chunk.exitCode, status: chunk.status, ms: Date.now() - started });
        } else if (chunk.kind === 'error') {
          // Aborts get the friendly i18n label; everything else surfaces the
          // raw error code + message so admins can diagnose.
          if (chunk.errorCode === 'Aborted') {
            setError(t('shell.aborted'));
          } else {
            setError(`${chunk.errorCode}: ${chunk.errorMessage}`);
          }
        } else {
          setChunks((prev) => [...prev, chunk]);
        }
      }
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setRunning(false);
      setAbortCtrl(null);
    }
  }, [canRun, runtimeArn, target.sessionId, command, timeout, config.region]);

  const onStop = useCallback(() => {
    abortCtrl?.abort();
  }, [abortCtrl]);

  const onCopy = useCallback(() => {
    const text = chunksToPlainText(chunks);
    // eslint-disable-next-line @typescript-eslint/no-floating-promises
    navigator.clipboard.writeText(text);
  }, [chunks]);

  const onPickHistory = (e: React.ChangeEvent<HTMLSelectElement>) => {
    if (e.target.value) setCommand(e.target.value);
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal shell-modal" onClick={(e) => e.stopPropagation()}>
        <div className="shell-modal-header">
          <h3>{t('shell.title').replace('{sessionId}', target.sessionId)}</h3>
          <button className="btn btn-sm btn-secondary" onClick={onClose}>
            {t('shell.close')}
          </button>
        </div>

        <div className="shell-modal-row">
          <label className="shell-field-label">{t('shell.fieldRuntime')}</label>
          <label className="shell-radio">
            <input
              type="radio"
              name="runtime"
              checked={runtime === 'text'}
              onChange={() => setRuntime('text')}
              disabled={running}
            />
            {t('shell.fieldRuntimeText')}
          </label>
          <label className="shell-radio">
            <input
              type="radio"
              name="runtime"
              checked={runtime === 'voice'}
              onChange={() => setRuntime('voice')}
              disabled={running || !config.voiceAgentRuntimeArn}
            />
            {t('shell.fieldRuntimeVoice')}
          </label>
        </div>

        <div className="shell-modal-row">
          <label className="shell-field-label">{t('shell.fieldSession')}</label>
          <code className="shell-session-id">{target.sessionId}</code>
        </div>

        <div className="shell-modal-row">
          <label className="shell-field-label">{t('shell.fieldCommand')}</label>
          <textarea
            className="shell-command-textarea"
            rows={4}
            value={command}
            onChange={(e) => setCommand(e.target.value)}
            disabled={running}
            placeholder="ls -la /var/task"
          />
        </div>
        <div className="shell-byte-counter">
          {t('shell.byteCounter')
            .replace('{bytes}', String(bytes))
            .replace('{max}', String(MAX_COMMAND_BYTES))}
        </div>

        <div className="shell-modal-row">
          <label className="shell-field-label">{t('shell.fieldTimeout')}</label>
          <input
            type="range"
            min={1}
            max={MAX_TIMEOUT_SECONDS}
            step={1}
            value={timeout}
            onChange={(e) => setTimeoutValue(parseInt(e.target.value, 10))}
            disabled={running}
          />
          <span className="shell-timeout-value">{timeout}s</span>
        </div>

        <div className="shell-modal-row">
          <label className="shell-field-label">{t('shell.historyLabel')}</label>
          <select
            className="shell-history-select"
            disabled={running || history.length === 0}
            value=""
            onChange={onPickHistory}
          >
            <option value="">
              {history.length === 0 ? t('shell.historyEmpty') : '—'}
            </option>
            {history.map((h, i) => (
              <option key={i} value={h}>
                {h.length > 80 ? h.slice(0, 80) + '…' : h}
              </option>
            ))}
          </select>
        </div>

        <div className="shell-modal-row shell-actions">
          <button className="btn btn-primary" onClick={onRun} disabled={!canRun}>
            {t('shell.run')}
          </button>
          <button className="btn btn-danger" onClick={onStop} disabled={!running}>
            {t('shell.stop')}
          </button>
          <button className="btn btn-secondary" onClick={onCopy} disabled={chunks.length === 0}>
            {t('shell.copy')}
          </button>
        </div>

        {validationErr && command.length > 0 && (
          <div className="alert alert-error">
            {t(`shell.validation.${validationErr}`)}
          </div>
        )}

        {error && <div className="alert alert-error">{error}</div>}

        <AnsiOutput chunks={chunks} />

        {exit && (
          <div className="shell-exit-footer">
            {t('shell.exitLine')
              .replace('{code}', String(exit.code))
              .replace('{status}', exit.status)
              .replace('{ms}', String(exit.ms))}
          </div>
        )}
      </div>
    </div>
  );
};

export default ShellModal;
