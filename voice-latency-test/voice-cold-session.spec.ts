/**
 * Voice cold-startup probe — SESSION-level cold (no runtime update).
 *
 * Goal: measure the "container is hot, but session is new" cold start that
 * a real user feels when they open voice mode after some idle time. Runtime
 * stays deployed throughout; we only StopRuntimeSession on every live voice
 * session between rounds so each click→WS fires a brand-new BidiAgent /
 * Nova Sonic init path.
 *
 * Flow (once per test run):
 *   1. Launch browser, goto chatbot, login ONCE.
 *   2. Loop N rounds:
 *      a. Stop all live runtime sessions via boto3 (aws CLI child_process).
 *      b. Click voice button.
 *      c. Record: click → ws_created → 101 → first_server_frame →
 *         first_audio (welcome clip's first chunk).
 *      d. Click voice button again to stop client-side.
 *      e. Short idle before next round.
 *
 * Env vars:
 *   CHATBOT_URL              required
 *   VOICE_RUNTIME_ARN        required — full runtime ARN, used for
 *                            StopRuntimeSession calls via AWS CLI
 *   AWS_REGION               required — region for AWS CLI
 *   PROBE_USERNAME           default admin@smarthome.local
 *   PROBE_PASSWORD           default SmartHome#Admin1
 *   PROBE_ROUNDS             default 100
 *   PROBE_OUTPUT             default ./results/tokyo-cold-session.jsonl
 *   PROBE_INTER_RUN_MS       default 500
 *   SKILLS_TABLE             default smarthome-skills — used to enumerate
 *                            active session IDs.
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
  path.join(__dirname, "results", "tokyo-cold-session.jsonl");
const INTER_RUN_MS = parseInt(process.env.PROBE_INTER_RUN_MS ?? "500", 10);
const SKILLS_TABLE = process.env.SKILLS_TABLE ?? "smarthome-skills";

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
  stop_sessions_ms?: number;       // how long stop_session loop took
  sessions_stopped?: number;
  click_to_ws_create_ms?: number;
  ws_handshake_ms?: number;
  ws_to_first_frame_ms?: number;
  ws_to_first_audio_ms?: number;
  total_click_to_first_frame_ms?: number;
  total_click_to_first_audio_ms?: number;
  session_id?: string;
}

function appendRecord(r: RunRecord) {
  fs.mkdirSync(path.dirname(OUTPUT), { recursive: true });
  fs.appendFileSync(OUTPUT, JSON.stringify(r) + "\n");
}

/**
 * Enumerate active voice session IDs from DynamoDB (__session_voice__
 * rows) and StopRuntimeSession each one. Returns {stopped, elapsedMs}.
 *
 * We spawn a short Python script each call — avoids pulling the AWS SDK
 * into the probe's node_modules. The Python one-liner uses boto3 which
 * the project venv already has installed.
 */
function stopAllVoiceSessions(): { stopped: number; elapsedMs: number } {
  const script = `
import os, sys, json, boto3
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
  const python = process.env.PROBE_PYTHON ?? "python3";
  const out = execFileSync(python, ["-c", script], {
    env: {
      ...process.env,
      AWS_REGION,
      VOICE_RUNTIME_ARN,
      SKILLS_TABLE,
    },
    encoding: "utf-8",
    stdio: ["ignore", "pipe", "inherit"],
  });
  const stopped = parseInt(out.trim() || "0", 10);
  return { stopped, elapsedMs: Date.now() - t0 };
}

const INSTRUMENTATION = `window.__probe = {};`;

test.describe.configure({ mode: "serial" });

test(`voice cold-session probe x${ROUNDS}`, async () => {
  // Each round: stop-session (~1s) + fresh-cold WS (~7s) + 101→audio (~1s)
  // + inter-run gap + potential recovery (re-login ~8s). Budget 45s per round.
  test.setTimeout(Math.max(600_000, ROUNDS * 45_000));

  const browser = await chromium.launch({
    headless: true,
    args: [
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
    ],
  });

  // Context + page are mutable so we can rebuild them if the tab crashes
  // after a chatbot reconnect storm (a known chatbot bug with fixed
  // sessionId). Each rebuild re-runs login — slower per round but keeps
  // the batch going.
  let context = await browser.newContext({ permissions: ["microphone"] });
  let page = await context.newPage();
  await page.addInitScript(INSTRUMENTATION);

  async function freshLogin(): Promise<void> {
    console.log("[login] navigate + sign in...");
    await page.goto(CHATBOT_URL, { waitUntil: "load", timeout: 30_000 });
    await page.waitForSelector('input[autocomplete="username"]', {
      timeout: 20_000,
    });
    await page.fill('input[autocomplete="username"]', USERNAME);
    await page.fill('input[autocomplete="current-password"]', PASSWORD);
    await page.click('button[type="submit"]');
    await page.waitForSelector("textarea", { timeout: 20_000 });
    // Let warmup POST finish so the runtime is hot before next round.
    await page.waitForTimeout(3000);
  }

  async function rebuildContext(): Promise<void> {
    console.log("[recover] rebuilding browser context...");
    // Use Promise.race to bound context.close() — it sometimes hangs when
    // the target page crashed.
    try {
      await Promise.race([
        context.close(),
        new Promise((r) => setTimeout(r, 5_000)),
      ]);
    } catch {
      /* ignore */
    }
    context = await browser.newContext({ permissions: ["microphone"] });
    page = await context.newPage();
    await page.addInitScript(INSTRUMENTATION);
    await freshLogin();
  }

  try {
    await freshLogin();
    console.log("[login] app ready");

    const btnSelector =
      'button[aria-label*="voice" i], button[aria-label*="语音"], button[title*="voice" i], button[title*="语音"]';
    await page.waitForSelector(btnSelector, { timeout: 10_000 });

    for (let i = 0; i < ROUNDS; i++) {
      const runId = `tokyo-cold-${Date.now()}-r${i}`;
      const record: RunRecord = {
        run_id: runId,
        ts_iso: new Date().toISOString(),
        round: i,
        ok: false,
      };

      // Step 1: stop any live sessions from the previous round (or from an
      // earlier test run that left stragglers in DDB).
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

      try {
        await runOneRound(page, record, btnSelector);
        record.ok = true;
      } catch (err) {
        record.error = (err as Error).message || String(err);
      } finally {
        appendRecord(record);
        console.log(JSON.stringify(record));
      }

      if (i < ROUNDS - 1) {
        await new Promise((r) => setTimeout(r, INTER_RUN_MS));
      }

      // Recovery: if this round errored, the chatbot likely crashed after a
      // reconnect on a stale sessionId. Rebuild the browser context so the
      // next round starts clean (Cognito re-login every time is slower but
      // reliable — keeps the batch going).
      if (!record.ok && i < ROUNDS - 1) {
        try {
          await rebuildContext();
          await page.waitForSelector(btnSelector, { timeout: 15_000 });
        } catch (e) {
          console.log(`[recover] rebuildContext failed: ${(e as Error).message}`);
          // Try once more with a longer wait.
          await new Promise((r) => setTimeout(r, 10_000));
          try {
            await rebuildContext();
          } catch (e2) {
            console.log(`[recover] second attempt failed: ${(e2 as Error).message}`);
          }
        }
      }
    }
  } finally {
    await context.close();
    await browser.close();
  }
});

async function runOneRound(
  page: Page,
  record: RunRecord,
  btnSelector: string,
): Promise<void> {
  const cdp: CDPSession = await page.context().newCDPSession(page);
  await cdp.send("Network.enable");

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
    // Welcome clip is delivered as {"type":"bidi_audio_stream","is_welcome":true}
    // (see agent/voice_session.py::_welcome_stream). Nova Sonic's reply audio
    // would use "audio_output" / "bidi_audio_output" — we match any audio-like
    // frame so the probe is robust to protocol tweaks.
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

  const clickAt = Date.now();
  await page.click(btnSelector);

  // Wait up to 30s for the first audio frame.
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
  if (wsFirstFrameAt !== null)
    record.total_click_to_first_frame_ms = wsFirstFrameAt - clickAt;
  if (wsFirstAudioAt !== null)
    record.total_click_to_first_audio_ms = wsFirstAudioAt - clickAt;

  if (sessionIdSeen) record.session_id = sessionIdSeen;

  // Tear down voice client-side so the next stop_session round has a real
  // session row to act on. Also: after a server-side stop_session the
  // chatbot's VoiceClient can get into a bad state. Full page reload is
  // the reliable reset — we keep Cognito tokens in localStorage so the
  // app re-hydrates to chat-ready without a re-login.
  await page.waitForTimeout(300);
  try {
    await page.click(btnSelector, { force: true, timeout: 2000 });
  } catch {
    /* may already be torn down */
  }
  await page.waitForTimeout(200);

  // Defensive reload so next round starts from a clean chatbot state.
  try {
    await page.reload({ waitUntil: "load", timeout: 20_000 });
    await page.waitForSelector("textarea", { timeout: 15_000 });
    await page.waitForSelector(btnSelector, { timeout: 10_000 });
    // Give warmup POST time to finish.
    await page.waitForTimeout(2_000);
  } catch {
    /* recovery loop in the caller will rebuild context */
  }
}
