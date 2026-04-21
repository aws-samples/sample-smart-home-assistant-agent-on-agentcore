/**
 * SigV4 helpers for the chatbot:
 *   - `signedFetch(...)`: SigV4-signs an HTTP POST to /invocations and fetches.
 *   - `presignWsUrl(...)`: returns a wss:// URL whose query string contains
 *     the SigV4 signature, so a browser WebSocket can connect without setting
 *     custom headers.
 *
 * Both target `service = "bedrock-agentcore"` in our configured region and use
 * temporary credentials obtained via the authenticated Cognito Identity Pool
 * role (see ../auth/CognitoAuth.ts#getAwsCredentials).
 */
import { SignatureV4 } from '@aws-sdk/signature-v4';
import { HttpRequest } from '@aws-sdk/protocol-http';
import { Sha256 } from '@aws-crypto/sha256-browser';
import type { AwsCredentialIdentity } from '@aws-sdk/types';

const SERVICE = 'bedrock-agentcore';

function makeSigner(creds: AwsCredentialIdentity, region: string): SignatureV4 {
  return new SignatureV4({
    service: SERVICE,
    region,
    credentials: creds,
    sha256: Sha256,
  });
}

function runtimeHost(region: string): string {
  return `bedrock-agentcore.${region}.amazonaws.com`;
}

function encodedArnPath(agentRuntimeArn: string, suffix: 'invocations' | 'ws'): string {
  return `/runtimes/${encodeURIComponent(agentRuntimeArn)}/${suffix}`;
}

/**
 * Build and execute a SigV4-signed POST /invocations call. Returns the raw
 * fetch Response so the caller can stream / parse as it wishes.
 *
 * `extraHeaders` lets us pass through the user's idToken as a custom header
 * (X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken) for the agent → gateway
 * per-user Cedar passthrough.
 */
export async function signedInvocationsFetch(params: {
  agentRuntimeArn: string;
  region: string;
  credentials: AwsCredentialIdentity;
  sessionId: string;
  body: unknown;
  extraHeaders?: Record<string, string>;
}): Promise<Response> {
  const { agentRuntimeArn, region, credentials, sessionId, body, extraHeaders = {} } = params;
  const host = runtimeHost(region);
  const path = encodedArnPath(agentRuntimeArn, 'invocations');
  const bodyStr = JSON.stringify(body);

  const req = new HttpRequest({
    method: 'POST',
    protocol: 'https:',
    hostname: host,
    path,
    headers: {
      host,
      'content-type': 'application/json',
      'x-amzn-bedrock-agentcore-runtime-session-id': sessionId,
      ...Object.fromEntries(Object.entries(extraHeaders).map(([k, v]) => [k.toLowerCase(), v])),
    },
    body: bodyStr,
  });

  const signer = makeSigner(credentials, region);
  const signed = await signer.sign(req);

  return fetch(`https://${host}${path}`, {
    method: 'POST',
    headers: signed.headers as Record<string, string>,
    body: bodyStr,
  });
}

/**
 * Build a SigV4 *presigned* wss:// URL that a browser WebSocket can open
 * directly.
 *
 * Subtle bit: browsers can't set custom request headers on a WebSocket
 * handshake, only Host is guaranteed to arrive. The AWS SDK JS signer will
 * hoist `x-amz-*`-prefixed headers into the query string automatically, but
 * leaves `x-amzn-*` headers in the header list. If we signed session-id or
 * our custom AuthToken as a header, the server would expect those headers at
 * handshake time and reject the signature when they're missing (browsers
 * send neither). So we put them in the query string ourselves, where SigV4
 * signs them into the canonical query string and the server can read them
 * back from the URL.
 */
export async function presignWsUrl(params: {
  agentRuntimeArn: string;
  region: string;
  credentials: AwsCredentialIdentity;
  sessionId: string;
  expiresSeconds?: number;
  extraQueryParams?: Record<string, string>;
}): Promise<string> {
  const {
    agentRuntimeArn,
    region,
    credentials,
    sessionId,
    expiresSeconds = 300,
    extraQueryParams = {},
  } = params;

  const host = runtimeHost(region);
  const path = encodedArnPath(agentRuntimeArn, 'ws');

  const req = new HttpRequest({
    method: 'GET',
    protocol: 'https:',
    hostname: host,
    path,
    headers: {
      // Host is the only header a browser is guaranteed to send on the WS
      // handshake, so it's the only thing we sign as a header.
      host,
    },
    query: {
      // Session-id + AuthToken travel as signed query params.
      'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': sessionId,
      ...extraQueryParams,
    },
  });

  const signer = makeSigner(credentials, region);
  const signed = await signer.presign(req, { expiresIn: expiresSeconds });

  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(signed.query || {})) {
    if (v == null) continue;
    if (Array.isArray(v)) v.forEach((vv) => qs.append(k, vv));
    else qs.append(k, v);
  }
  return `wss://${host}${path}?${qs.toString()}`;
}
