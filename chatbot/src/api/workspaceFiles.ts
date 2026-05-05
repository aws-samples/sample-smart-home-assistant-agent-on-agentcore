/**
 * Workspace file browser — lists and downloads files under
 * /mnt/workspace/<sessionId>/ by calling InvokeAgentRuntimeCommand
 * directly from the browser, same pattern as the admin console's Remote
 * Shell (see docs/architecture-and-design.md §9.10). No Lambda, no API
 * Gateway — purely frontend + IAM on the Cognito authenticated role.
 */
import { getAwsCredentials } from '../auth/CognitoAuth';
import { getConfig } from '../config';

export interface WorkspaceEntry {
  name: string;
  isDir: boolean;
  size: number;
  mtime: string;
}

export interface WorkspaceFileContent {
  path: string;
  sizeBytes: number;
  mime: string;
  base64: string;
}

const WORKSPACE_PREFIX = '/mnt/workspace/';
const CONTENT_CAP_BYTES = 5 * 1024 * 1024;

function assertWorkspacePath(path: string): void {
  if (!path.startsWith(WORKSPACE_PREFIX)) {
    throw new Error('path must start with /mnt/workspace/');
  }
  if (path.split('/').includes('..')) {
    throw new Error('path traversal rejected');
  }
}

function singleQuote(s: string): string {
  // POSIX-safe shell quoting: 'foo'"'"'bar' — wraps in single quotes and
  // escapes any embedded single quote with '"'"'. Same trick `shlex.quote`
  // uses server-side.
  return `'${s.replace(/'/g, `'"'"'`)}'`;
}

async function runOne(
  runtimeArn: string,
  sessionId: string,
  command: string,
  timeoutSeconds = 15,
): Promise<{ stdout: string; stderr: string; exitCode: number | null }> {
  const sdk = await import('@aws-sdk/client-bedrock-agentcore');
  const config = getConfig();
  const credentials = await getAwsCredentials();
  const client = new sdk.BedrockAgentCoreClient({
    region: config.region,
    credentials,
  });
  const cmd = new sdk.InvokeAgentRuntimeCommandCommand({
    agentRuntimeArn: runtimeArn,
    runtimeSessionId: sessionId,
    body: { command, timeout: timeoutSeconds },
  });
  const resp = await client.send(cmd);
  if (!resp.stream) throw new Error('no response stream');

  let stdout = '';
  let stderr = '';
  let exitCode: number | null = null;
  for await (const evt of resp.stream as AsyncIterable<any>) {
    const chunk = evt.chunk;
    if (chunk?.contentDelta) {
      if (chunk.contentDelta.stdout) stdout += chunk.contentDelta.stdout;
      if (chunk.contentDelta.stderr) stderr += chunk.contentDelta.stderr;
    }
    if (chunk?.contentStop) {
      exitCode = chunk.contentStop.exitCode ?? null;
    }
    for (const key of [
      'validationException',
      'accessDeniedException',
      'resourceNotFoundException',
      'throttlingException',
      'internalServerException',
      'runtimeClientError',
    ]) {
      const node = (evt as any)[key];
      if (node) throw new Error(`${key}: ${node.message || node.reason || key}`);
    }
  }
  return { stdout, stderr, exitCode };
}

export async function listWorkspace(
  sessionId: string,
  path: string,
): Promise<{ path: string; entries: WorkspaceEntry[] }> {
  assertWorkspacePath(path);
  const runtimeArn = getConfig().agentRuntimeArn;
  if (!runtimeArn) throw new Error('agentRuntimeArn not configured');
  // The runtime container's execve of `command` is quirky — a lone `cd`
  // inside the command string falls over with "cd: too many arguments",
  // and some coreutils long-options (--time-style, --quoting-style) are
  // rejected. Run through `bash -c` and use plain `ls -la` so the parser
  // sees the standard 9-column output.
  const qp = singleQuote(path);
  const cmd = `bash -c 'if [ -d ${qp} ]; then ls -lA ${qp}; else echo __NOTDIR__ 1>&2; exit 1; fi'`;
  const r = await runOne(runtimeArn, sessionId, cmd, 10);
  if (r.exitCode && r.exitCode !== 0) {
    throw new Error(r.stderr || r.stdout || `list failed (exit ${r.exitCode})`);
  }
  const entries: WorkspaceEntry[] = [];
  for (const line of r.stdout.split('\n')) {
    if (!line.trim() || line.startsWith('total ')) continue;
    // Standard GNU ls -la line: perms links user group size <date> name
    // The date format varies (e.g. "May  4 10:25" or "Jan  5  2025"); pick
    // the 9th whitespace-token onward as the name.
    const parts = line.split(/\s+/);
    if (parts.length < 9) continue;
    const mode = parts[0];
    const size = parseInt(parts[4], 10) || 0;
    const name = parts.slice(8).join(' ');
    entries.push({
      name,
      isDir: mode.startsWith('d'),
      size,
      mtime: `${parts[5]} ${parts[6]} ${parts[7]}`,
    });
  }
  return { path, entries };
}

export async function fetchWorkspaceFile(
  sessionId: string,
  path: string,
): Promise<WorkspaceFileContent> {
  assertWorkspacePath(path);
  const runtimeArn = getConfig().agentRuntimeArn;
  if (!runtimeArn) throw new Error('agentRuntimeArn not configured');
  const qp = singleQuote(path);
  const cmd = `bash -c 'if [ -f ${qp} ]; then stat -c "__STAT__ size=%s" ${qp}; head -c ${CONTENT_CAP_BYTES} ${qp} | base64 -w0; else echo "not a regular file" 1>&2; exit 1; fi'`;
  const r = await runOne(runtimeArn, sessionId, cmd, 20);
  if (r.exitCode && r.exitCode !== 0) {
    throw new Error(r.stderr || `content fetch failed (exit ${r.exitCode})`);
  }
  let size = 0;
  let b64 = '';
  for (const line of r.stdout.split('\n')) {
    if (line.startsWith('__STAT__')) {
      const m = line.match(/size=(\d+)/);
      if (m) size = parseInt(m[1], 10);
    } else {
      b64 += line;
    }
  }
  const ext = (path.split('.').pop() || '').toLowerCase();
  const mimeByExt: Record<string, string> = {
    png: 'image/png',
    jpg: 'image/jpeg',
    jpeg: 'image/jpeg',
    webp: 'image/webp',
    gif: 'image/gif',
    txt: 'text/plain',
    md: 'text/markdown',
    json: 'application/json',
    html: 'text/html',
  };
  return {
    path,
    sizeBytes: size,
    mime: mimeByExt[ext] || 'application/octet-stream',
    base64: b64.trim(),
  };
}
