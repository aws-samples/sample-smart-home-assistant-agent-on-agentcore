/**
 * Browser wrapper over the AgentCore Runtime `InvokeAgentRuntimeCommand` API.
 *
 * Streams shell-exec output from inside the runtime container back to the
 * admin console. The SDK module is lazy-imported so its ~100 KB of minified
 * code only downloads when the Remote Shell modal first opens.
 *
 * Normalises the SDK's union-typed event stream into a flat ShellChunk
 * async-iterable so the React component never sees AWS SDK types directly.
 */
import type { AwsCredentialIdentity } from '@aws-sdk/types';

export interface RunCommandOptions {
  agentRuntimeArn: string;
  runtimeSessionId: string;
  command: string;
  timeoutSeconds: number;
  credentials: AwsCredentialIdentity;
  region: string;
  signal?: AbortSignal;
}

export type ShellChunk =
  | { kind: 'start' }
  | { kind: 'stdout'; text: string }
  | { kind: 'stderr'; text: string }
  | { kind: 'exit'; exitCode: number; status: string }
  | { kind: 'error'; errorCode: string; errorMessage: string };

/**
 * Async-iterable over normalised chunks. Caller drives with `for await`.
 * Yields at most one `start`, N stdout/stderr, and exactly one terminal
 * event (`exit` or `error`). After a terminal event the generator returns.
 */
export async function* runCommand(
  opts: RunCommandOptions,
): AsyncGenerator<ShellChunk> {
  // Lazy-load the SDK so the admin bundle stays small for users who never
  // open the Shell modal.
  const sdk = await import('@aws-sdk/client-bedrock-agentcore');
  const client = new sdk.BedrockAgentCoreClient({
    region: opts.region,
    credentials: opts.credentials,
  });
  const command = new sdk.InvokeAgentRuntimeCommandCommand({
    agentRuntimeArn: opts.agentRuntimeArn,
    runtimeSessionId: opts.runtimeSessionId,
    body: { command: opts.command, timeout: opts.timeoutSeconds },
  });
  // Abort surfaces in multiple shapes depending on where in the pipeline the
  // cancel fires: AbortError from the fetch layer, DOMException named
  // AbortError, "BodyStreamBuffer was aborted" from the stream reader, and
  // a bare signal.aborted after an underlying socket close. Treat any
  // post-abort error as an Abort.
  const isAbort = (err: any): boolean =>
    opts.signal?.aborted ||
    err?.name === 'AbortError' ||
    err?.code === 'ABORT_ERR' ||
    /abort/i.test(err?.message || '');

  let resp;
  try {
    resp = await client.send(command, { abortSignal: opts.signal });
  } catch (err: any) {
    if (isAbort(err)) {
      yield { kind: 'error', errorCode: 'Aborted', errorMessage: 'aborted by user' };
      return;
    }
    yield {
      kind: 'error',
      errorCode: err?.name || 'SendError',
      errorMessage: err?.message || String(err),
    };
    return;
  }

  if (!resp.stream) {
    yield { kind: 'error', errorCode: 'NoStream', errorMessage: 'no response stream' };
    return;
  }

  try {
    for await (const evt of resp.stream) {
      if (opts.signal?.aborted) {
        yield { kind: 'error', errorCode: 'Aborted', errorMessage: 'aborted by user' };
        return;
      }
      const c = (evt as any).chunk;
      if (c?.contentStart) {
        yield { kind: 'start' };
        continue;
      }
      if (c?.contentDelta) {
        const stdout = c.contentDelta.stdout;
        const stderr = c.contentDelta.stderr;
        if (stdout) yield { kind: 'stdout', text: stdout };
        if (stderr) yield { kind: 'stderr', text: stderr };
        continue;
      }
      if (c?.contentStop) {
        yield {
          kind: 'exit',
          exitCode: c.contentStop.exitCode ?? -1,
          status: c.contentStop.status ?? 'UNKNOWN',
        };
        return;
      }
      // Error variants arrive as discriminated fields on the event, not under chunk.
      const errorVariants: Array<[string, string]> = [
        ['validationException', 'ValidationException'],
        ['accessDeniedException', 'AccessDeniedException'],
        ['resourceNotFoundException', 'ResourceNotFoundException'],
        ['serviceQuotaExceededException', 'ServiceQuotaExceededException'],
        ['throttlingException', 'ThrottlingException'],
        ['internalServerException', 'InternalServerException'],
        ['runtimeClientError', 'RuntimeClientError'],
      ];
      for (const [key, code] of errorVariants) {
        const node = (evt as any)[key];
        if (node) {
          yield {
            kind: 'error',
            errorCode: code,
            errorMessage: node.message || node.reason || code,
          };
          return;
        }
      }
    }
  } catch (err: any) {
    if (isAbort(err)) {
      yield { kind: 'error', errorCode: 'Aborted', errorMessage: 'aborted by user' };
      return;
    }
    yield {
      kind: 'error',
      errorCode: err?.name || 'StreamError',
      errorMessage: err?.message || String(err),
    };
  }
}
