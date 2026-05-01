/**
 * Vision-latency probe — fresh login per iteration.
 *
 * Each iteration:
 *   1. StopRuntimeSession on this user's text session (drops warm container).
 *   2. Launch fresh Chromium context → clean localStorage/cookies.
 *   3. Login → chatbot warms up runtime in background (parallel text+voice).
 *   4. Attach N images (from vision-latency-test/images, cycled deterministically).
 *   5. Click send. Time from click → first non-empty agent bubble.
 *   6. Append one JSONL row to PROBE_OUTPUT.
 *   7. Sleep PROBE_INTER_RUN_MS (default 10s) to avoid Bedrock throttling.
 *
 * Env vars:
 *   CHATBOT_URL             required
 *   TEXT_RUNTIME_ARN        required — full ARN; stop_session target
 *   AWS_REGION              required
 *   PROBE_IMAGE_COUNT       1|2|3 (required)
 *   PROBE_MODEL             "haiku" | "nova" — only used as a tag in JSONL;
 *                           the runtime env VISION_MODEL_ID is what actually
 *                           decides routing (flipped by run.py).
 *   PROBE_ROUNDS            default 30
 *   PROBE_OUTPUT            default ./results/<model>_<count>.jsonl
 *   PROBE_USERNAME          default admin@smarthome.local
 *   PROBE_PASSWORD          default SmartHome#Admin1
 *   PROBE_INTER_RUN_MS      default 10000
 *   PROBE_PYTHON            default python3
 */
import { test, chromium, Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

function requireEnv(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required env: ${name}`);
  return v;
}

const CHATBOT_URL = requireEnv("CHATBOT_URL");
const TEXT_RUNTIME_ARN = requireEnv("TEXT_RUNTIME_ARN");
const AWS_REGION = requireEnv("AWS_REGION");
const IMAGE_COUNT = parseInt(requireEnv("PROBE_IMAGE_COUNT"), 10);
const MODEL_TAG = process.env.PROBE_MODEL ?? "unknown";
const ROUNDS = parseInt(process.env.PROBE_ROUNDS ?? "30", 10);
const OUTPUT =
  process.env.PROBE_OUTPUT ??
  path.join(__dirname, "results", `${MODEL_TAG}_${IMAGE_COUNT}.jsonl`);
const USERNAME = process.env.PROBE_USERNAME ?? "admin@smarthome.local";
const PASSWORD = process.env.PROBE_PASSWORD ?? "SmartHome#Admin1";
const INTER_RUN_MS = parseInt(process.env.PROBE_INTER_RUN_MS ?? "10000", 10);
const PROBE_PYTHON = process.env.PROBE_PYTHON ?? "python3";

const IMAGES_DIR = path.join(__dirname, "images");
const IMAGE_FILES = fs.readdirSync(IMAGES_DIR)
  .filter((n) => n.endsWith(".jpg") || n.endsWith(".png") || n.endsWith(".webp"))
  .sort()
  .map((n) => path.join(IMAGES_DIR, n));
if (IMAGE_FILES.length < IMAGE_COUNT) {
  throw new Error(`Not enough images in ${IMAGES_DIR}: have ${IMAGE_FILES.length}, need ${IMAGE_COUNT}`);
}

function pickImages(round: number, count: number): string[] {
  // Deterministic per-round rotation. Round 0 uses imgs [0..count-1],
  // round 1 uses [1..count], etc. Keeps the set varied but reproducible.
  const out: string[] = [];
  for (let i = 0; i < count; i++) {
    out.push(IMAGE_FILES[(round + i) % IMAGE_FILES.length]);
  }
  return out;
}

interface RunRecord {
  ts_iso: string;
  round: number;
  model: string;
  image_count: number;
  ok: boolean;
  error?: string;
  images: string[];
  stop_session_ms?: number;
  login_to_ready_ms?: number;
  warmup_ms?: number;
  click_to_reply_ms?: number;
  reply_chars?: number;
}

function appendRow(r: RunRecord) {
  fs.mkdirSync(path.dirname(OUTPUT), { recursive: true });
  fs.appendFileSync(OUTPUT, JSON.stringify(r) + "\n");
}

function stopUserTextSession(): number {
  // Stop the admin user's text session by computing the sessionId from the
  // user's Cognito sub. Reliable because session-id is derived deterministically
  // (see chatbot ChatInterface.tsx: `user-session-${sub}`). We look up the sub
  // from Cognito on the fly.
  const script = `
import os, boto3
region = os.environ['AWS_REGION']
arn = os.environ['TEXT_RUNTIME_ARN']
username = os.environ['PROBE_USERNAME']
# Derive sessionId from Cognito sub (admin-api _resolve_user_for_session inverse).
cog = boto3.client('cognito-idp', region_name=region)
# Brute find the user pool from the runtime stack? We take it from env if set.
# Simpler: list sessions on the runtime and stop them all — the probe is single-user.
dp = boto3.client('bedrock-agentcore', region_name=region)
# AgentCore doesn't expose list_sessions directly on the dataplane for text runtime;
# instead, read DynamoDB skills table which the agent writes on each invocation.
import os
ddb = boto3.resource('dynamodb', region_name=region).Table(os.environ.get('SKILLS_TABLE','smarthome-skills'))
resp = ddb.scan(FilterExpression='skillName = :s',
                ExpressionAttributeValues={':s': '__session_text__'},
                ProjectionExpression='sessionId')
count = 0
for it in resp.get('Items', []):
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
  execFileSync(PROBE_PYTHON, ["-c", script], {
    env: { ...process.env, AWS_REGION, TEXT_RUNTIME_ARN, PROBE_USERNAME: USERNAME },
    encoding: "utf-8",
    stdio: ["ignore", "pipe", "inherit"],
  });
  return Date.now() - t0;
}

async function loginAndReady(page: Page): Promise<{ login_to_ready_ms: number; warmup_ms: number }> {
  // Install fetch instrumentation *before* navigating so we see warmup POSTs.
  await page.addInitScript(() => {
    (window as any).__posts = [];
    const orig = window.fetch;
    window.fetch = function (input: any, init: any) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      let idx = -1;
      if (url.includes("/invocations")) {
        idx = (window as any).__posts.push({ url, startedAt: Date.now(), status: "pending", body: init?.body ? String(init.body).slice(0, 40) : "" }) - 1;
      }
      return orig.apply(this, arguments as any).then((resp: Response) => {
        if (idx >= 0) {
          (window as any).__posts[idx].endedAt = Date.now();
          (window as any).__posts[idx].status = resp.status;
        }
        return resp;
      }, (err: any) => {
        if (idx >= 0) {
          (window as any).__posts[idx].endedAt = Date.now();
          (window as any).__posts[idx].status = "err:" + (err?.message || err);
        }
        throw err;
      });
    } as any;
  });

  const t0 = Date.now();
  await page.goto(CHATBOT_URL, { waitUntil: "domcontentloaded" });
  await page.locator('input[autocomplete="username"]').first().fill(USERNAME);
  await page.locator('input[type="password"]').first().fill(PASSWORD);
  await Promise.all([
    page.locator('button[type="submit"]').first().click(),
    page.waitForSelector('button[aria-label="Attach image"]', { timeout: 30_000 }),
  ]);
  const login_to_ready_ms = Date.now() - t0;

  // Wait for the TEXT-runtime warmup POST to complete. The chatbot fires
  // warmups to both the text and voice runtimes in parallel; the voice one
  // returns first because its runtime has already warmed on prior rounds.
  // We need the text-runtime container to be hot or the real vision POST
  // will race it and get a 424. Match by the text runtime substring in the URL.
  // Use the text runtime's unique trailing segment (after "runtime/") — that
  // differs from the voice runtime so we can match precisely.
  const textRuntimeId = TEXT_RUNTIME_ARN.split("/").pop() || TEXT_RUNTIME_ARN;
  const textRuntimeUrlPart = encodeURIComponent(textRuntimeId);
  const warmupStart = Date.now();
  await page.waitForFunction(
    (urlPart: string) => {
      const posts = (window as any).__posts || [];
      return posts.some(
        (p: any) =>
          p.body && p.body.includes("__warmup__") &&
          p.url && p.url.includes(urlPart) &&
          p.status !== "pending",
      );
    },
    textRuntimeUrlPart,
    { timeout: 120_000 },
  );
  const warmup_ms = Date.now() - warmupStart;
  return { login_to_ready_ms, warmup_ms };
}

async function sendImagesAndTime(page: Page, imagePaths: string[]): Promise<{ click_to_reply_ms: number; reply_chars: number }> {
  // Count existing agent bubbles to distinguish the new reply from any that
  // might appear during warmup (none should, but be safe).
  const initialAgentCount = await page.locator('.bubble-agent').count();

  // Click paperclip, attach all files in one file-chooser.
  const [chooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    page.locator('button[aria-label="Attach image"]').click(),
  ]);
  await chooser.setFiles(imagePaths);

  // Wait for thumbnails to render so we know state is committed.
  await page.waitForFunction(
    (expected) => document.querySelectorAll(".image-strip-item").length === expected,
    imagePaths.length,
    { timeout: 5000 },
  );

  await page.locator("textarea.message-input").fill("describe this image in 2 sentences");

  // Time from click to first non-empty agent bubble.
  const t0 = Date.now();
  await page.locator('button[aria-label="Send message"]').click();

  // Wait for a new agent bubble whose text is non-empty and not the typing indicator.
  await page.waitForFunction(
    (initial) => {
      const bubbles = document.querySelectorAll(".bubble-agent .message-text");
      if (bubbles.length <= initial) return false;
      const last = bubbles[bubbles.length - 1] as HTMLElement;
      const txt = (last.innerText || "").trim();
      return txt.length > 0;
    },
    initialAgentCount,
    { timeout: 120_000 },
  );
  const click_to_reply_ms = Date.now() - t0;

  const reply_chars = await page.evaluate(() => {
    const bubbles = document.querySelectorAll(".bubble-agent .message-text");
    const last = bubbles[bubbles.length - 1] as HTMLElement;
    return (last.innerText || "").length;
  });

  return { click_to_reply_ms, reply_chars };
}

async function dumpPostsOnFailure(page: Page): Promise<string> {
  try {
    const posts = await page.evaluate(() => (window as any).__posts || []);
    return JSON.stringify(posts);
  } catch {
    return "[instrumentation unavailable]";
  }
}

test.describe.configure({ mode: "serial" });

test(`vision probe ${MODEL_TAG} × ${IMAGE_COUNT}img × ${ROUNDS}`, async () => {
  test.setTimeout(ROUNDS * 180_000);
  const browser = await chromium.launch();

  for (let round = 0; round < ROUNDS; round++) {
    const row: RunRecord = {
      ts_iso: new Date().toISOString(),
      round,
      model: MODEL_TAG,
      image_count: IMAGE_COUNT,
      ok: false,
      images: pickImages(round, IMAGE_COUNT).map((p) => path.basename(p)),
    };

    try {
      row.stop_session_ms = stopUserTextSession();
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      try {
        const lr = await loginAndReady(page);
        row.login_to_ready_ms = lr.login_to_ready_ms;
        row.warmup_ms = lr.warmup_ms;
        const paths = pickImages(round, IMAGE_COUNT);
        try {
          const { click_to_reply_ms, reply_chars } = await sendImagesAndTime(page, paths);
          row.click_to_reply_ms = click_to_reply_ms;
          row.reply_chars = reply_chars;
          row.ok = true;
        } catch (inner: any) {
          (row as any).debug_posts = await dumpPostsOnFailure(page);
          throw inner;
        }
      } finally {
        await ctx.close();
      }
    } catch (e: any) {
      row.error = String(e?.message || e);
    }

    // (Instrumentation dump is captured during the happy path; failures
    // already leave enough info in the JSONL error field. We don't re-open
    // the closed context.)

    appendRow(row);
    // eslint-disable-next-line no-console
    console.log(`[${row.round + 1}/${ROUNDS}] ${row.ok ? "ok" : "FAIL"} ${row.click_to_reply_ms ?? "-"}ms${row.error ? " err=" + row.error.slice(0, 80) : ""}`);

    if (round < ROUNDS - 1) {
      await new Promise((r) => setTimeout(r, INTER_RUN_MS));
    }
  }

  await browser.close();
});
