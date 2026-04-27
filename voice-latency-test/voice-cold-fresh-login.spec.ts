/**
 * Voice cold-startup probe — FRESH LOGIN each round.
 *
 * Goal: reproduce the *true* end-to-end experience of a user who opens the
 * chatbot, logs in, and clicks voice for the first time after a cold runtime.
 * Measures optimisations that improve the whole startup path (warmup
 * parallelisation, pre-signed WS URL, etc.) — optimisations that
 * voice-cold-session.spec.ts cannot see because it keeps one long-lived
 * login + page throughout.
 *
 * Flow (once per test run):
 *   Loop N rounds:
 *     1. StopRuntimeSession on every live voice session (drop warm workers).
 *     2. UpdateAgentRuntime bump a nonce env var (force runtime version roll).
 *     3. Wait runtime → READY.
 *     4. Launch a FRESH browser context (clears localStorage, cookies, cache).
 *     5. Navigate to chatbot, fill Cognito form, submit.
 *     6. Wait for chat UI, click voice button.
 *     7. Measure click → WS created → 101 → first server frame → first audio.
 *     8. Tear down browser context; next round.
 *
 * This is slow (~1 minute per round — runtime update ~15s + fresh login + WS
 * cold + teardown) but it's the only faithful reproduction of "what does
 * a first-time user feel?" for the whole stack.
 *
 * Env vars (same conventions as voice-cold-session.spec.ts):
 *   CHATBOT_URL              required
 *   VOICE_RUNTIME_ARN        required — full ARN, used for Update + StopSession
 *   AWS_REGION               required
 *   PROBE_USERNAME           default admin@smarthome.local
 *   PROBE_PASSWORD           default SmartHome#Admin1
 *   PROBE_ROUNDS             default 100
 *   PROBE_OUTPUT             default ./results/fresh-login.jsonl
 *   PROBE_INTER_RUN_MS       default 2000  (on top of ~15s runtime update wait)
 *   PROBE_PYTHON             default python3 — used to spawn AWS sidecar
 *   SKILLS_TABLE             default smarthome-skills
 */

import { test, chromium, CDPSession, Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

const CHATBOT_URL = requireEnv("CHATBOT_URL");
const VOICE_RUNTIME_ARN = requireEnv("VOICE_RUNTIME_ARN");
const AWS_REGION = requireEnv("AWS_REGION");
const USERNAME = process.env.PROBE_USERNAME ?? "admin@smarthome.local";
const PASSWORD = process.env.PROBE_PASSWORD ?? "SmartHome#Admin1";
const ROUNDS = parseInt(process.env.PROBE_ROUNDS ?? "100", 10);
const OUTPUT =
  process.env.PROBE_OUTPUT ??
  path.join(__dirname, "results", "fresh-login.jsonl");
const INTER_RUN_MS = parseInt(process.env.PROBE_INTER_RUN_MS ?? "2000", 10);
const SKILLS_TABLE = process.env.SKILLS_TABLE ?? "smarthome-skills";
const PROBE_PYTHON = process.env.PROBE_PYTHON ?? "python3";

// Derive runtime ID from ARN. Format: arn:aws:bedrock-agentcore:<r>:<a>:runtime/<id>
const VOICE_RUNTIME_ID = VOICE_RUNTIME_ARN.split("/").slice(-1)[0];

function requireEnv(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required env var: ${name}`);
  return v;
}

interface RunRecord {
  run_id: string;
  ts_iso: string;
  round: number;
  ok: boolean;
  error?: string;
  stop_sessions_ms?: number;
  sessions_stopped?: number;
  update_runtime_ms?: number;        // Update + wait READY
  page_load_ms?: number;             // navigation start → load event
  login_ms?: number;                 // click signIn → chat UI mounted
  warmup_text_ms?: number;           // observed via fetch() proxy
  warmup_voice_ms?: number;
  click_to_ws_create_ms?: number;
  ws_handshake_ms?: number;
  ws_to_first_frame_ms?: number;
  ws_to_first_audio_ms?: number;
  total_click_to_first_audio_ms?: number;
  total_login_to_first_audio_ms?: number;  // full user journey after submit
  session_id?: string;
}

function appendRecord(r: RunRecord) {
  fs.mkdirSync(path.dirname(OUTPUT), { recursive: true });
  fs.appendFileSync(OUTPUT, JSON.stringify(r) + "\n");
}

/**
 * 1-step: stop all live voice sessions. Same script as voice-cold-session.
 */
function stopAllVoiceSessions(): { stopped: number; elapsedMs: number } {
  const script = `
import os, boto3
region = os.environ['AWS_REGION']
arn = os.environ['VOICE_RUNTIME_ARN']
table_name = os.environ['SKILLS_TABLE']
dp = boto3.client('bedrock-agentcore', region_name=region)
ddb = boto3.resource('dynamodb', region_name=region)
t = ddb.Table(table_name)
items = t.scan(
  FilterExpression='skillName = :s',
  ExpressionAttributeValues={':s': '__session_voice__'},
  ProjectionExpression='sessionId',
).get('Items', [])
count = 0
for it in items:
  sid = it.get('sessionId')
  if not sid: continue
  try:
    dp.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=sid)
    count += 1
  except dp.exceptions.ResourceNotFoundException:
    count += 1
  except Exception:
    pass
print(count)
`;
  const t0 = Date.now();
  const out = execFileSync(PROBE_PYTHON, ["-c", script], {
    env: { ...process.env, AWS_REGION, VOICE_RUNTIME_ARN, SKILLS_TABLE },
    encoding: "utf-8",
    stdio: ["ignore", "pipe", "inherit"],
  });
  return { stopped: parseInt(out.trim() || "0", 10), elapsedMs: Date.now() - t0 };
}

/**
 * 2-step: bump nonce + wait READY. Rolls container version so the next
 * invoke is on a fresh worker. Preserves requestHeaderConfiguration so MCP
 * JWT forwarding keeps working (force-cold bug we hit earlier).
 */
function bumpRuntimeNonce(): { elapsedMs: number } {
  const script = `
import os, time, sys, boto3
region = os.environ['AWS_REGION']
rt_id = os.environ['VOICE_RUNTIME_ID']
c = boto3.client('bedrock-agentcore-control', region_name=region)
rt = c.get_agent_runtime(agentRuntimeId=rt_id)
env = dict(rt.get('environmentVariables') or {})
env['LATENCY_PROBE_COLD_NONCE'] = str(int(time.time()*1000))
kw = {'agentRuntimeId': rt_id, 'environmentVariables': env}
for k in ('agentRuntimeArtifact','roleArn','networkConfiguration',
          'protocolConfiguration','authorizerConfiguration'):
    if k in rt and rt[k] is not None: kw[k] = rt[k]
# requestHeaderConfiguration round-trip: boto3 returns it either nested
# under requestHeaderConfiguration OR at top-level depending on the
# AgentCore API version / boto3 model. Read BOTH, preserve first non-empty.
rhc = rt.get('requestHeaderConfiguration') or {}
allowlist = rhc.get('requestHeaderAllowlist') or rt.get('requestHeaderAllowlist')
if allowlist:
    kw['requestHeaderConfiguration'] = {'requestHeaderAllowlist': allowlist}
c.update_agent_runtime(**kw)
t0 = time.time()
while time.time()-t0 < 300:
    if c.get_agent_runtime(agentRuntimeId=rt_id)['status'] == 'READY':
        print(f'{time.time()-t0:.3f}')
        sys.exit(0)
    time.sleep(3)
sys.exit(3)
`;
  const t0 = Date.now();
  execFileSync(PROBE_PYTHON, ["-c", script], {
    env: { ...process.env, AWS_REGION, VOICE_RUNTIME_ID },
    encoding: "utf-8",
    stdio: ["ignore", "pipe", "inherit"],
  });
  return { elapsedMs: Date.now() - t0 };
}

// Instrument window.fetch so we can time chatbot warmup POSTs per round.
const INSTRUMENTATION = `
  window.__probe = { warmupStart: {}, warmupEnd: {} };
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    let key = null;
    if (url.includes('/runtimes/') && url.includes('/invocations')) {
      key = url.includes('smarthomevoice_') ? 'voice' : 'text';
      if (!window.__probe.warmupStart[key]) {
        window.__probe.warmupStart[key] = performance.now();
      }
    }
    return origFetch.apply(this, arguments).then((resp) => {
      if (key && !window.__probe.warmupEnd[key]) {
        window.__probe.warmupEnd[key] = performance.now();
      }
      return resp;
    });
  };
`;

test.describe.configure({ mode: "serial" });

test(`voice fresh-login cold probe x${ROUNDS}`, async () => {
  // Per-round budget: stop_session 1s + update+READY ~15-60s + fresh login 10s
  // + cold WS 7s + teardown 2s. Budget 90s/round with head-room.
  test.setTimeout(Math.max(900_000, ROUNDS * 90_000));

  const browser = await chromium.launch({
    headless: true,
    args: [
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
    ],
  });

  try {
    for (let i = 0; i < ROUNDS; i++) {
      const runId = `fresh-login-${Date.now()}-r${i}`;
      const record: RunRecord = {
        run_id: runId,
        ts_iso: new Date().toISOString(),
        round: i,
        ok: false,
      };

      // ---- 1. stop sessions ----
      try {
        const s = stopAllVoiceSessions();
        record.stop_sessions_ms = s.elapsedMs;
        record.sessions_stopped = s.stopped;
      } catch (err) {
        record.error = `stop_session failed: ${(err as Error).message}`;
        appendRecord(record);
        console.log(JSON.stringify(record));
        continue;
      }

      // ---- 2. nonce-bump runtime + wait READY ----
      try {
        const u = bumpRuntimeNonce();
        record.update_runtime_ms = u.elapsedMs;
      } catch (err) {
        record.error = `update_runtime failed: ${(err as Error).message}`;
        appendRecord(record);
        console.log(JSON.stringify(record));
        continue;
      }

      // ---- 3. fresh browser context + login + voice ----
      let context = null as Awaited<ReturnType<typeof browser.newContext>> | null;
      try {
        context = await browser.newContext({ permissions: ["microphone"] });
        const page = await context.newPage();
        await page.addInitScript(INSTRUMENTATION);

        await runFreshLoginRound(page, record);
        record.ok = true;
      } catch (err) {
        record.error = (err as Error).message || String(err);
      } finally {
        if (context) {
          try {
            await Promise.race([
              context.close(),
              new Promise((r) => setTimeout(r, 5_000)),
            ]);
          } catch {
            /* ignore */
          }
        }
        appendRecord(record);
        console.log(JSON.stringify(record));
      }

      if (i < ROUNDS - 1) {
        await new Promise((r) => setTimeout(r, INTER_RUN_MS));
      }
    }
  } finally {
    await browser.close();
  }
});

async function runFreshLoginRound(page: Page, record: RunRecord): Promise<void> {
  const cdp: CDPSession = await page.context().newCDPSession(page);
  await cdp.send("Network.enable");

  // CDP WS-event timestamps in wall-clock ms.
  let wsCreatedAt: number | null = null;
  let wsHandshakeAckAt: number | null = null;
  let wsFirstFrameAt: number | null = null;
  let wsFirstAudioAt: number | null = null;
  let wsReqId: string | null = null;
  let sessionIdSeen: string | null = null;

  cdp.on("Network.webSocketCreated", (ev) => {
    if (ev.url.includes("bedrock-agentcore") || ev.url.includes("runtimes/")) {
      wsCreatedAt = Date.now();
      wsReqId = ev.requestId;
      try {
        const u = new URL(ev.url);
        const sid =
          u.searchParams.get("X-Amzn-Bedrock-AgentCore-Runtime-Session-Id") ??
          u.searchParams.get("x-amzn-bedrock-agentcore-runtime-session-id");
        if (sid) sessionIdSeen = sid;
      } catch {
        /* ignore */
      }
    }
  });
  cdp.on("Network.webSocketHandshakeResponseReceived", (ev) => {
    if (ev.requestId !== wsReqId) return;
    wsHandshakeAckAt = Date.now();
  });
  cdp.on("Network.webSocketFrameReceived", (ev) => {
    if (ev.requestId !== wsReqId) return;
    if (wsFirstFrameAt === null) wsFirstFrameAt = Date.now();
    const payload = ev.response.payloadData;
    if (typeof payload !== "string") return;
    if (
      wsFirstAudioAt === null &&
      (payload.includes('"bidi_audio_stream"') ||
        payload.includes('"welcome_audio"') ||
        payload.includes('"bidi_audio_output"') ||
        payload.includes('"audio_output"'))
    ) {
      wsFirstAudioAt = Date.now();
    }
  });

  // ---- page load ----
  const tNavStart = Date.now();
  const resp = await page.goto(CHATBOT_URL, { waitUntil: "load", timeout: 30_000 });
  if (!resp?.ok()) throw new Error(`chatbot goto failed: ${resp?.status()}`);
  record.page_load_ms = Date.now() - tNavStart;

  // ---- login ----
  await page.waitForSelector('input[autocomplete="username"]', { timeout: 20_000 });
  await page.fill('input[autocomplete="username"]', USERNAME);
  await page.fill('input[autocomplete="current-password"]', PASSWORD);
  const tLogin = Date.now();
  await page.click('button[type="submit"]');
  await page.waitForSelector("textarea", { timeout: 20_000 });
  record.login_ms = Date.now() - tLogin;

  // ---- wait for warmup POSTs to complete (both runtimes) ----
  try {
    await page.waitForFunction(
      () =>
        (window as any).__probe?.warmupEnd?.text &&
        (window as any).__probe?.warmupEnd?.voice,
      null,
      { timeout: 30_000 },
    );
    const w = await page.evaluate(() => ({
      s: (window as any).__probe.warmupStart,
      e: (window as any).__probe.warmupEnd,
    }));
    if (w.s.text && w.e.text) record.warmup_text_ms = Math.round(w.e.text - w.s.text);
    if (w.s.voice && w.e.voice) record.warmup_voice_ms = Math.round(w.e.voice - w.s.voice);
  } catch {
    // warmup may not fire if chatbot changed its startup — non-fatal.
  }

  // ---- click voice ----
  const btnSelector =
    'button[aria-label*="voice" i], button[aria-label*="语音"], button[title*="voice" i], button[title*="语音"]';
  await page.waitForSelector(btnSelector, { timeout: 10_000 });

  const clickAt = Date.now();
  await page.click(btnSelector);

  // ---- wait for first audio ----
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (wsFirstAudioAt !== null) break;
    await page.waitForTimeout(100);
  }

  if (wsCreatedAt !== null) record.click_to_ws_create_ms = wsCreatedAt - clickAt;
  if (wsCreatedAt !== null && wsHandshakeAckAt !== null)
    record.ws_handshake_ms = wsHandshakeAckAt - wsCreatedAt;
  if (wsHandshakeAckAt !== null && wsFirstFrameAt !== null)
    record.ws_to_first_frame_ms = wsFirstFrameAt - wsHandshakeAckAt;
  if (wsHandshakeAckAt !== null && wsFirstAudioAt !== null)
    record.ws_to_first_audio_ms = wsFirstAudioAt - wsHandshakeAckAt;
  if (wsFirstAudioAt !== null) {
    record.total_click_to_first_audio_ms = wsFirstAudioAt - clickAt;
    record.total_login_to_first_audio_ms = wsFirstAudioAt - tLogin;
  }

  if (sessionIdSeen) record.session_id = sessionIdSeen;

  // Tear down voice client-side before context close.
  await page.waitForTimeout(300);
  try {
    await page.click(btnSelector, { force: true, timeout: 2000 });
  } catch {
    /* ignore */
  }
}
