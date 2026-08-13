import React, { useState, useEffect, useRef, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { jwtDecode } from 'jwt-decode';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Textarea from '@cloudscape-design/components/textarea';
import { getConfig } from '../config';
import { getIdToken, getAwsCredentials } from '../auth/CognitoAuth';
import { useI18n } from '../i18n';
import { VoiceClient } from '../voice/VoiceClient';
import { signedInvocationsFetch, signedGatewayInvocationsFetch, presignWsUrl } from '../voice/sigv4';
import BrowserPanel, { PanelTab } from './BrowserPanel';
import { fetchActiveBrowserSession, BrowserSessionInfo } from '../api/browserSessions';
import { fetchActiveCodeSession, CodeSessionInfo } from '../api/codeSessions';
import { getTenantMode } from '../api/tenantEnv';
import { submitFeedback } from '../api/feedback';
import PromptExamples, { welcomeChips } from './PromptExamples';
import { fetchChatHistory } from '../api/chatHistory';

const CUSTOM_AUTH_HEADER = 'X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken';

const IMAGE_MIME_ALLOWLIST = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];
// How many prior turns to restore on login. 20 is the requirement; it is also
// about where a chat window stops being scannable, and every turn restored is a
// turn the model carries in context.
const HISTORY_TURNS = 20;
const MAX_IMAGES_PER_MESSAGE = 3;
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;

interface ChatMessage {
  id: string;
  role: 'user' | 'agent';
  content: string;
  timestamp: Date;
  // Nova Sonic emits each transcript twice (SPECULATIVE then FINAL). The
  // bubble starts with `pending: true` so the FINAL pass can replace it in
  // place by finding the most-recent pending message for the same role.
  // Keeping this flag on the message (instead of tracking IDs in a ref)
  // makes the setMessages updater pure — which matters because React 18
  // StrictMode double-invokes updaters, and impure side effects (like
  // mutating a ref to a freshly-generated id) caused pending lookups to
  // miss so the 2nd utterance overwrote the 1st.
  pending?: boolean;
  // Nova Sonic `completionId` — stable across SPECULATIVE + FINAL content
  // blocks of the same utterance, so the reducer merges the two passes into
  // one bubble. Distinct utterances get distinct completionIds and render as
  // separate bubbles. (contentId would NOT work: Nova Sonic assigns
  // different contentIds to the SPEC and FINAL blocks of one reply.)
  completionId?: string;
  // Blob URLs for the user's attached images, created per-message so they
  // survive clearing the input-area thumbnail strip. Revoked on unmount.
  imageUrls?: string[];
  // Which tools this turn actually used, in order, as readable labels (spec 5 S6).
  // Collected from the progress stream rather than queried afterwards: the events
  // already arrive for the "asking the …" indicator, so the trace is free and
  // instant. Reading it back from `aws/spans` would mean a 10-20s Logs Insights
  // query per turn to learn what the stream just said.
  //
  // Only present on turns that streamed, which is why the panel is absent rather
  // than empty on the image and A/B paths.
  trace?: string[];
}

async function fileToBase64(file: File): Promise<string> {
  const buf = await file.arrayBuffer();
  const bytes = new Uint8Array(buf);
  // Chunk to avoid `btoa` argument overflow on large files (20 MB).
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + CHUNK)));
  }
  return btoa(binary);
}

function generateId(): string {
  return Math.random().toString(36).substring(2) + Date.now().toString(36);
}

/**
 * A tool name as something worth showing a waiting user.
 *
 * `a2a_home_security_agent_risk_assessment` -> `Home Security specialist`.
 * `query_sensor_history` -> `Sensor History`.
 * Gateway tools arrive prefixed (`SmartHomeDeviceQuery___query_device_state`),
 * so the target prefix is dropped first.
 *
 * The A2A case is the one that matters: a delegated turn is the slow one, and
 * naming the specialist is the whole point — "asking the Home Security
 * specialist…" is the honest version of a 30-second wait.
 */
export function toolLabel(rawName: string): string {
  if (!rawName) return '';
  const name = rawName.includes('___') ? rawName.split('___').pop()! : rawName;
  const a2a = name.match(/^a2a_(.+?)_agent_/);
  if (a2a) {
    return `${titleCase(a2a[1].replace(/_/g, ' '))} specialist`;
  }
  return titleCase(name.replace(/_/g, ' '));
}

// Initialism fixups. Plain title-casing turns `knowledge_qa` into "Knowledge Qa",
// which reads as a typo in the one place a developer is looking closely.
const INITIALISMS: Record<string, string> = { qa: 'QA', led: 'LED', tv: 'TV',
                                              pm25: 'PM2.5', co2: 'CO2' };

function titleCase(words: string): string {
  return words
    .split(' ')
    .map((w) => INITIALISMS[w.toLowerCase()] ||
                w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

/**
 * Read the runtime's SSE reply, reporting each tool as it starts, and return the
 * final answer text.
 *
 * Frames are `data: {json}\n\n`. Two kinds: `progress` (a tool the model just
 * called) and `answer` (the reply, or an `error`). The answer always arrives, so
 * a stream that ends without one means the connection dropped mid-turn and that
 * is reported rather than shown as an empty reply.
 *
 * Buffers by blank line rather than by chunk: a frame can be split across TCP
 * reads, and parsing per chunk would drop the tail of a split frame — which for
 * the `answer` frame means losing the whole reply.
 */
async function readAgentStream(
  response: Response,
  onTool: (label: string) => void,
): Promise<string> {
  const body = response.body;
  if (!body) throw new Error('no response stream');
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffered = '';
  let answer: string | null = null;

  const handleFrame = (frame: string) => {
    const line = frame.split('\n').find((l) => l.startsWith('data:'));
    if (!line) return;
    let evt: any;
    try {
      evt = JSON.parse(line.slice(5).trim());
      // AgentCore JSON-encodes each value the handler yields, so a frame decodes
      // to the STRING '{"type":"progress",...}' rather than to the object.
      // Measured against the live runtime — parsing once leaves a string, and
      // `evt.type` on a string is undefined, so every frame including the answer
      // would have been silently dropped.
      if (typeof evt === 'string') evt = JSON.parse(evt);
    } catch {
      return; // a keep-alive or a shape we do not know; ignore rather than fail
    }
    if (!evt || typeof evt !== 'object') return;
    if (evt.type === 'progress' && evt.tool) onTool(toolLabel(evt.tool));
    else if (evt.type === 'answer') {
      if (evt.error) throw new Error(evt.error);
      answer = evt.response ?? '';
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffered += decoder.decode(value, { stream: true });
    let split: number;
    while ((split = buffered.indexOf('\n\n')) !== -1) {
      const frame = buffered.slice(0, split);
      buffered = buffered.slice(split + 2);
      handleFrame(frame);
    }
  }
  if (buffered.trim()) handleFrame(buffered);

  if (answer === null) throw new Error('the reply ended before the agent answered');
  return answer;
}

const ChatInterface: React.FC = () => {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // How many leading messages came from AgentCore Memory rather than from this
  // login. Drives the "earlier conversation" divider, and is a count rather than a
  // flag on each message so the divider can sit BETWEEN the history and the first
  // new turn without every bubble carrying presentation state.
  const [historyCount, setHistoryCount] = useState(0);
  const [historyNote, setHistoryNote] = useState('');
  const [inputValue, setInputValue] = useState('');
  const [isTyping, setIsTyping] = useState(false);
  // The tool the agent is currently waiting on, as a label. Replaces the static
  // "thinking…" during a delegated turn, where the wait is ~30s and the
  // orchestrator names its specialist ~6s before any prose exists (spec 5 S5).
  const [progressTool, setProgressTool] = useState('');
  const [error, setError] = useState('');
  const [voiceActive, setVoiceActive] = useState(false);
  const [voiceStatus, setVoiceStatus] = useState('');
  const [attachedImages, setAttachedImages] = useState<
    Array<{ file: File; previewUrl: string; id: string }>
  >([]);
  const [imageErrors, setImageErrors] = useState<string[]>([]);
  const [browserSession, setBrowserSession] = useState<BrowserSessionInfo | null>(null);
  // The user identifier used for /browser-sessions polling — same resolution
  // rule as the runtime (email → cognito:username → sub). Captured on the
  // first sendMessage so the polling effect has a stable value.
  const [browserUserId, setBrowserUserId] = useState<string | null>(null);
  const [browserAgentSessionId, setBrowserAgentSessionId] = useState<string | null>(null);
  // The Browser panel is always rendered on the right as a narrow
  // vertical rail; clicking either rail label expands it into the full
  // 720px view on that tab. The ✕ in the expanded panel only collapses
  // back to the rail, so the user always has one-click re-entry.
  const [browserPanelExpanded, setBrowserPanelExpanded] = useState(false);
  const [browserPanelTab, setBrowserPanelTab] = useState<PanelTab>('live');
  // The example library. Openable at any point in a conversation, unlike the
  // welcome-screen chips it supplements: those live behind
  // `messages.length === 0` and so disappeared for good after the first message,
  // taking the only visible list of the agent's capabilities with them.
  const [examplesOpen, setExamplesOpen] = useState(false);
  // Which turns have been voted on, and how. Kept in component state rather than
  // re-read from the API: the vote is idempotent per turn (the sort key embeds
  // the turn id) so the only thing the UI needs is which arrow to light up.
  const [votes, setVotes] = useState<Record<string, 'up' | 'down'>>({});
  // The turn whose 👎 reason box is open. One at a time: a reason belongs to a
  // specific turn, and several open boxes invite typing into the wrong one.
  const [reasonFor, setReasonFor] = useState('');
  const [reasonText, setReasonText] = useState('');
  // Code Interpreter run for the current turn, polled the same way as the
  // browser session. Unlike the browser panel, a fresh code run auto-expands
  // the panel on the CodeInterpreter tab (requested behavior).
  const [codeSession, setCodeSession] = useState<CodeSessionInfo | null>(null);
  const codeAutoOpenedRef = useRef<string | null>(null);
  // Timestamp of the most recent sendMessage — used by the polling effect
  // to drop any DDB rows from previous runs (startedAt < this) so the
  // panel doesn't render a dead session while waiting for the new
  // tool call to write its `running` row.
  const sendStartAtRef = useRef<number>(0);
  const { t, language } = useI18n();

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const shellRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const voiceClientRef = useRef<VoiceClient | null>(null);
  const warmupRef = useRef(false);
  // Pre-signed voice WS URL cached from the post-login warmup. Saves the
  // SigV4 presign + Identity Pool creds fetch (~200-400ms) on the first
  // voice button tap. AgentCore presigned URLs are valid for 5 min; we
  // expire ours at 4 min to be safe.
  const presignedWsRef = useRef<{ url: string; expiresAt: number } | null>(null);

  // Per-login runtimeSessionId. Reused by warmup, text chat, voice, and
  // browser-use so they all land on the same microVM (no cold start after
  // warmup). The `${Date.now()}` suffix gives each page load a fresh
  // session window, which keeps AgentCore Online Evaluation's trace
  // batches short and prevents historical traces from poisoning new
  // evaluations.
  const loginSessionIdRef = useRef<string | null>(null);
  const loginSessionIdFor = useCallback((sub: string) => {
    if (!loginSessionIdRef.current) {
      loginSessionIdRef.current = `user-session-${sub}-${Date.now()}`;
    }
    return loginSessionIdRef.current;
  }, []);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, isTyping, scrollToBottom]);

  // Bound the chat shell to the viewport, so `.messages-area` scrolls INSIDE it
  // instead of the whole page growing.
  //
  // The CSS alone could not do this. `.messages-area { flex: 1; overflow-y: auto }`
  // only scrolls if some ancestor has a definite height, and the nearest ones are
  // Cloudscape's AppLayout grid — which sets `min-height` and no height, so
  // `flex: 1` resolved against CONTENT. Measured before the fix: viewport 493px,
  // messages-area 740px, document 935px, and `scrollHeight === clientHeight` on the
  // scroller — i.e. the inner scrollbar never engaged and the transcript simply
  // extended the page. `scrollIntoView` then scrolled the document, moving the
  // header off-screen rather than advancing the list.
  //
  // Done in JS rather than `calc(100vh - 49px)` because the top nav's height is not
  // a constant: TopNavigation collapses utilities into an overflow menu at narrow
  // widths, and a hardcoded offset is wrong the moment it re-flows. Cloudscape does
  // publish its own offset custom properties, but they carry a build-time hash
  // (`--awsui-offset-top-6b9ypa`), so app code cannot reference them by name — the
  // same constraint documented for the simulator's theme tokens.
  useEffect(() => {
    const fit = () => {
      const shell = shellRef.current;
      if (!shell) return;
      // Measure the shell's OWN distance from the top of the viewport rather than
      // subtracting the nav height. They are not the same number: AppLayout puts
      // the content inside a container with 40px of bottom padding and a few px of
      // offset above, so `innerHeight - navHeight` overshot by 52px and the page
      // still scrolled ~50px. Reading `getBoundingClientRect().top` accounts for
      // every bit of that chrome without naming any of it — which matters because
      // those paddings live in hashed Cloudscape classes we cannot reference.
      //
      // Height is cleared first so the measurement is not taken against the value
      // this function set last time (that feeds back and the shell creeps).
      shell.style.height = '';
      const top = shell.getBoundingClientRect().top;
      // Padding BELOW the shell is read, not hardcoded, for the same reason the
      // nav height is: 40px today is a Cloudscape implementation detail, and a
      // literal here would silently mis-size the moment it changes.
      const parent = shell.parentElement;
      const below = parent
        ? parseFloat(getComputedStyle(parent).paddingBottom) || 0
        : 0;
      shell.style.height = `${Math.max(240, window.innerHeight - top - below)}px`;
    };
    fit();
    window.addEventListener('resize', fit);
    // The nav re-flows on its own (utilities collapsing, language switch changing
    // label widths) without a window resize, so observe it too.
    const nav = document.querySelector('#top-nav');
    const ro = nav ? new ResizeObserver(fit) : null;
    if (nav && ro) ro.observe(nav);
    return () => {
      window.removeEventListener('resize', fit);
      ro?.disconnect();
    };
  }, []);

  // Prefetch the AudioWorklet JS so the first tap on the voice button doesn't
  // block on a CloudFront round-trip. We don't call getUserMedia here because
  // it would trigger the mic permission prompt before the user asks for voice.
  useEffect(() => {
    fetch('/pcm-recorder-processor.js').catch(() => {
      // Best-effort prefetch; ignore network errors.
    });
  }, []);

  // Warm up BOTH AgentCore Runtimes (text + voice) immediately after login.
  // Share one idToken + creds fetch across both requests, then fire them in
  // parallel. Each runtime's agent short-circuits "__warmup__" without invoking
  // the LLM, so the cost is ~50ms per request but it heats the Python process
  // (imports, boto3 clients, DynamoDB connection pool).
  useEffect(() => {
    if (warmupRef.current) return;
    warmupRef.current = true;
    (async () => {
      try {
        const config = getConfig();
        const [token, creds] = await Promise.all([getIdToken(), getAwsCredentials()]);
        const decoded = jwtDecode<{ sub: string; email?: string; 'cognito:username'?: string }>(token);
        const userId = decoded.email || decoded['cognito:username'] || decoded.sub;
        // Share session.id with real chat turns so warmup heats the same
        // microVM that serves them (AgentCore Runtime isolates microVMs
        // per runtimeSessionId).
        const sessionId = loginSessionIdFor(decoded.sub);

        const targets = [config.agentRuntimeArn];
        // Voice runtime is a separate ARN; fall back to text ARN if the deploy
        // hasn't been run post-split yet (transition safety).
        if (config.voiceAgentRuntimeArn && config.voiceAgentRuntimeArn !== config.agentRuntimeArn) {
          targets.push(config.voiceAgentRuntimeArn);
        }

        // X-Amzn-Trace-Id with Sampled=0 keeps the warmup turn out of the
        // sampled trace window. The root Starlette span still makes it to
        // aws/spans because LOCAL_ROOT spans are exported regardless, but
        // the rest of the warmup request is suppressed.
        const warmupTraceId = (() => {
          const ts = Math.floor(Date.now() / 1000).toString(16);
          const uniq = Array.from(crypto.getRandomValues(new Uint8Array(12)))
            .map(b => b.toString(16).padStart(2, '0')).join('');
          return `Root=1-${ts}-${uniq};Sampled=0`;
        })();

        await Promise.allSettled(targets.map((arn) =>
          signedInvocationsFetch({
            agentRuntimeArn: arn,
            region: config.region,
            credentials: creds,
            sessionId,
            body: { prompt: '__warmup__', userId },
            extraHeaders: {
              [CUSTOM_AUTH_HEADER]: token,
              'X-Amzn-Trace-Id': warmupTraceId,
            },
          })
        ));

        // Pre-presign the voice WS URL with the same session id so the
        // first voice tap skips the 200-400ms presign roundtrip.
        const voiceArn = config.voiceAgentRuntimeArn || config.agentRuntimeArn;
        if (voiceArn) {
          try {
            const wsUrl = await presignWsUrl({
              agentRuntimeArn: voiceArn,
              region: config.region,
              credentials: creds,
              sessionId,
              expiresSeconds: 300,
              extraQueryParams: { [CUSTOM_AUTH_HEADER]: token },
            });
            presignedWsRef.current = {
              url: wsUrl,
              expiresAt: Date.now() + 4 * 60 * 1000,
            };
          } catch {
            // Presign prefetch is best-effort; startVoice will retry.
          }
        }
      } catch {
        // Warmup is best-effort; ignore failures.
      }
    })();
  }, []);

  // Restore the user's recent conversation from AgentCore Memory.
  //
  // Runs once on mount, and only prepends when the transcript is still empty — a
  // StrictMode double-invoke would otherwise render the history twice, and a slow
  // response arriving after the user has already typed must not push their first
  // message down the page.
  //
  // The model sees this same history: `memory_session_id` keys short-term memory on
  // the actor rather than on the per-login runtime session, so what is displayed
  // here IS the agent's context. That equivalence is the point — displaying a
  // transcript the model cannot see produces the worst kind of demo failure, where
  // the user asks about something visibly on screen and the agent does not know it.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { turns, sessionsRead } = await fetchChatHistory(HISTORY_TURNS);
        if (cancelled || turns.length === 0) return;
        setMessages((prev) => {
          if (prev.length > 0) return prev;
          setHistoryCount(turns.length);
          // Only worth saying when it spans logins; "restored from 1 session" is
          // noise about an implementation detail.
          setHistoryNote(sessionsRead > 1 ? String(sessionsRead) : '');
          return turns.map((turn, i) => ({
            id: `history-${i}-${turn.timestamp}`,
            role: turn.role === 'assistant' ? ('agent' as const) : ('user' as const),
            content: turn.text,
            timestamp: new Date(turn.timestamp || Date.now()),
          }));
        });
      } catch {
        // Best-effort: an empty chat window is a working chat window.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Capture the logged-in user's id + sessionId on mount. We retry a few
  // times because Cognito's getIdToken() can race its own refresh on the
  // very first page load (we saw the browser report a CORS error that was
  // actually a 401 on a not-yet-refreshed token).
  useEffect(() => {
    let cancelled = false;
    const resolveUser = async (attempt = 0): Promise<void> => {
      try {
        const token = await getIdToken();
        if (cancelled) return;
        const decoded = jwtDecode<{ sub: string; email?: string; 'cognito:username'?: string }>(token);
        const uid = decoded.email || decoded['cognito:username'] || decoded.sub;
        setBrowserUserId(uid);
        setBrowserAgentSessionId(loginSessionIdFor(decoded.sub));
      } catch {
        if (cancelled || attempt > 5) return;
        setTimeout(() => resolveUser(attempt + 1), 500);
      }
    };
    resolveUser();
    return () => { cancelled = true; };
  }, []);

  // Poll /sessions?action=browser-active while the agent is thinking, and
  // keep ticking for a short "tail" window after the turn ends so the
  // brief `running` DDB row (which is overwritten by `completed` within
  // ~30s of the tool finishing) is always captured.
  useEffect(() => {
    if (!browserUserId) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const s = await fetchActiveBrowserSession(browserUserId);
        if (cancelled) return;
        if (!s) return;
        // Drop rows from previous runs. Give a 5s grace before sendStartAt
        // to cover clock skew between browser and agent runtime.
        if (sendStartAtRef.current > 0 && s.startedAt) {
          const startedMs = new Date(s.startedAt).getTime();
          if (startedMs < sendStartAtRef.current - 5000) return;
        }
        setBrowserSession(s);
        // Don't auto-expand the panel — the rail is always visible and
        // the user decides when to open. A running session surfaces via
        // the polled row reaching BrowserPanel; no parent-state toggle
        // is needed here.
      } catch {
        // Transient auth / network errors are ignored.
      }
    };
    if (isTyping) {
      tick();
      const iv = window.setInterval(tick, 1500);
      return () => {
        cancelled = true;
        window.clearInterval(iv);
      };
    }
    // Not typing: do one final catch-up tick so the panel surfaces even
    // when the whole browse_web tool completes before React has re-run
    // this effect (happens when Kimi replies faster than the tool's
    // running → completed DDB write transition).
    tick();
    return () => { cancelled = true; };
  }, [isTyping, browserUserId]);

  // Poll /sessions?action=code-active alongside the browser poll. When a code
  // run for the CURRENT turn appears, auto-open the panel on the
  // CodeInterpreter tab (the user explicitly wanted the tab to pop open when
  // the tool is invoked). We auto-open at most once per run id.
  useEffect(() => {
    if (!browserUserId) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const s = await fetchActiveCodeSession(browserUserId);
        if (cancelled || !s) return;
        // Drop rows from previous turns (5s grace for clock skew), same filter
        // as the browser poll.
        if (sendStartAtRef.current > 0 && s.startedAt) {
          const startedMs = new Date(s.startedAt).getTime();
          if (startedMs < sendStartAtRef.current - 5000) return;
        }
        setCodeSession(s);
        if (codeAutoOpenedRef.current !== s.sessionId) {
          codeAutoOpenedRef.current = s.sessionId;
          setBrowserPanelTab('code');
          setBrowserPanelExpanded(true);
        }
      } catch {
        // Transient auth / network errors are ignored.
      }
    };
    if (isTyping) {
      tick();
      const iv = window.setInterval(tick, 1500);
      return () => { cancelled = true; window.clearInterval(iv); };
    }
    tick();
    return () => { cancelled = true; };
  }, [isTyping, browserUserId]);

  const addImages = useCallback((incoming: FileList | File[]) => {
    // Validate synchronously against the CURRENT state, then apply one
    // pure setState updater at the end. Mixing validation with the updater
    // breaks under React 18 StrictMode (double-invoked updaters re-push the
    // same errors or retarget new ones).
    const files = Array.from(incoming);
    const errs: string[] = [];
    const accepted: Array<{ file: File; previewUrl: string; id: string }> = [];
    let slotsLeft = MAX_IMAGES_PER_MESSAGE - attachedImages.length;
    for (const f of files) {
      if (!IMAGE_MIME_ALLOWLIST.includes(f.type)) {
        errs.push(t('chat.image.badFormat').replace('{name}', f.name));
        continue;
      }
      if (f.size > MAX_IMAGE_BYTES) {
        errs.push(t('chat.image.tooBig').replace('{name}', f.name));
        continue;
      }
      if (slotsLeft <= 0) {
        errs.push(t('chat.image.tooMany'));
        break;
      }
      accepted.push({ file: f, previewUrl: URL.createObjectURL(f), id: generateId() });
      slotsLeft -= 1;
    }
    if (accepted.length > 0) setAttachedImages((prev) => [...prev, ...accepted]);
    setImageErrors(errs);
    // Reset the input so selecting the same file again still fires onChange.
    if (fileInputRef.current) fileInputRef.current.value = '';
  }, [attachedImages.length, t]);

  const removeImage = useCallback((id: string) => {
    setAttachedImages((prev) => {
      const target = prev.find((x) => x.id === id);
      if (target) URL.revokeObjectURL(target.previewUrl);
      return prev.filter((x) => x.id !== id);
    });
  }, []);

  const sendMessage = useCallback(async () => {
    const text = inputValue.trim();
    if (!text && attachedImages.length === 0) return;

    // Snapshot the strip's Files/URLs: we're about to clear `attachedImages`
    // but need them for (a) base64 encoding and (b) bubble-long-lived URLs.
    const snapshotFiles = attachedImages.map((x) => x.file);
    const bubbleUrls = snapshotFiles.map((f) => URL.createObjectURL(f));
    // Revoke the strip URLs; the bubble owns its own copies now.
    attachedImages.forEach((x) => URL.revokeObjectURL(x.previewUrl));

    const userMessage: ChatMessage = {
      id: generateId(),
      role: 'user',
      content: text,
      timestamp: new Date(),
      imageUrls: bubbleUrls.length ? bubbleUrls : undefined,
    };

    setMessages((prev) => [...prev, userMessage]);
    setInputValue('');
    setAttachedImages([]);
    setImageErrors([]);
    setIsTyping(true);
    setError('');
    // Clear stale browser-session state so the panel doesn't show a
    // completed row from a previous run while polling races the new
    // tool call's DDB write. The polling effect below also guards on
    // sendStartAt so returned rows older than this send are ignored.
    setBrowserSession(null);
    setCodeSession(null);
    sendStartAtRef.current = Date.now();

    try {
      const config = getConfig();
      const token = await getIdToken();
      const creds = await getAwsCredentials();
      const decoded = jwtDecode<{ sub: string; email?: string; 'cognito:username'?: string }>(token);
      const userId = decoded.email || decoded['cognito:username'] || decoded.sub;
      const sessionId = loginSessionIdFor(decoded.sub);
      setBrowserUserId(userId);
      setBrowserAgentSessionId(sessionId);

      let images: Array<{ mediaType: string; data: string }> | undefined;
      if (snapshotFiles.length > 0) {
        const encoded = await Promise.all(
          snapshotFiles.map(async (f) => ({ mediaType: f.type, data: await fileToBase64(f) })),
        );
        images = encoded;
      }

      // Per-tenant entry environment (§8.13). Each tenant is in one of:
      //   default      → direct SigV4 to primary runtime, DDB additive prompts
      //   ab-bundles   → direct SigV4 to bundles runtime (BeforeModelCallEvent
      //                  hook overrides system_prompt from baggage)
      //   ab-targets   → optimization gateway (target-based runtime-version A/B)
      // Cache miss / fetch failure falls back to 'default' (see api/tenantEnv).
      // Resolved before the body is built because whether we ask for a streaming
      // reply depends on it.
      const mode = await getTenantMode(userId);

      const body: Record<string, unknown> = { prompt: text, userId };
      if (images) body.images = images;
      // Ask for progress events (spec 5 S5). The runtime replies with SSE instead
      // of a JSON body when this is set, so the reader below has to parse it.
      // Only for text turns: the image paths compose their reply from a vision
      // caption and have no tool calls to report.
      //
      // Not requested for the two A/B modes. `ab-bundles` runs a different
      // runtime and `ab-targets` goes through the optimization gateway, and
      // neither is guaranteed to pass a streaming body through unbuffered — an
      // experiment arm that silently degraded to a 30s blank wait would be worse
      // than no progress at all. They keep the JSON path.
      const wantStream = !images && mode === 'default';
      if (wantStream) body.stream = true;

      let response: Response;
      if (mode === 'ab-targets' && config.optimizationGatewayUrl) {
        response = await signedGatewayInvocationsFetch({
          gatewayUrl: config.optimizationGatewayUrl,
          targetName: config.optimizationDefaultTarget,
          region: config.region,
          credentials: creds,
          sessionId,
          body,
          extraHeaders: { [CUSTOM_AUTH_HEADER]: token },
        });
      } else if (mode === 'ab-bundles' && config.bundlesRuntimeArn) {
        response = await signedInvocationsFetch({
          agentRuntimeArn: config.bundlesRuntimeArn,
          region: config.region,
          credentials: creds,
          sessionId,
          body,
          extraHeaders: { [CUSTOM_AUTH_HEADER]: token },
        });
      } else {
        // 'default' mode (or unconfigured / config field empty for the
        // selected non-default mode → safe fallback to primary runtime).
        response = await signedInvocationsFetch({
          agentRuntimeArn: config.agentRuntimeArn,
          region: config.region,
          credentials: creds,
          sessionId,
          body,
          extraHeaders: { [CUSTOM_AUTH_HEADER]: token },
        });
      }

      if (!response.ok) {
        const errBody = await response.text();
        throw new Error(`Request failed (${response.status}): ${errBody}`);
      }

      let agentText: string;
      // Every tool this turn used, in order — kept for the trace panel below.
      const trace: string[] = [];
      if (wantStream) {
        agentText = await readAgentStream(response, (label) => {
          setProgressTool(label);
          trace.push(label);
        });
      } else {
        const data = await response.json();
        agentText = data.response || data.text || data.content || JSON.stringify(data);
      }

      setMessages((prev) => [
        ...prev,
        {
          id: generateId(),
          role: 'agent',
          content: agentText,
          timestamp: new Date(),
          trace: trace.length > 0 ? trace : undefined,
        },
      ]);
    } catch (err: any) {
      setError(err.message || t('chat.sendFailed'));
    } finally {
      setIsTyping(false);
      setProgressTool('');
    }
  }, [inputValue, attachedImages, t]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const formatTime = (date: Date): string => {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  const stopVoice = useCallback(() => {
    voiceClientRef.current?.stop();
    voiceClientRef.current = null;
    setVoiceActive(false);
    setVoiceStatus('');
  }, []);

  const startVoice = useCallback(async () => {
    setError('');
    setVoiceStatus(t('chat.voiceMode.connecting'));
    try {
      const config = getConfig();
      const token = await getIdToken();
      const creds = await getAwsCredentials();
      const decoded = jwtDecode<{ sub: string }>(token);
      const sessionId = loginSessionIdFor(decoded.sub);

      // Voice lives on its own AgentCore Runtime post-split. Fall back to the
      // text runtime ARN if voiceAgentRuntimeArn is empty (transitional state
      // while config.js catches up after a deploy).
      const voiceArn = config.voiceAgentRuntimeArn || config.agentRuntimeArn;
      // Reuse the WS URL pre-signed during login warmup when it's still
      // fresh. Saves ~200-400ms of SigV4 signing + Identity Pool creds work
      // on the first voice-button tap.
      let wsUrl: string;
      const cached = presignedWsRef.current;
      if (cached && cached.expiresAt > Date.now()) {
        wsUrl = cached.url;
        // Invalidate after one use — a presigned WS URL can only be consumed
        // once; subsequent taps need a fresh signature.
        presignedWsRef.current = null;
      } else {
        // The AuthToken flows as a query-string allowlisted custom header
        // per AgentCore WS docs so the agent can forward it to the MCP gateway.
        wsUrl = await presignWsUrl({
          agentRuntimeArn: voiceArn,
          region: config.region,
          credentials: creds,
          sessionId,
          expiresSeconds: 300,
          extraQueryParams: {
            [CUSTOM_AUTH_HEADER]: token,
          },
        });
      }

      const client = new VoiceClient({
        wsUrl,
        onStatus: (event) => {
          if (event.kind === 'connected') setVoiceStatus(t('chat.voiceMode.connected'));
          else if (event.kind === 'connecting') setVoiceStatus(t('chat.voiceMode.connecting'));
          else if (event.kind === 'disconnected') setVoiceStatus(t('chat.voiceMode.disconnected'));
          else if (event.kind === 'error') {
            setError(`${t('chat.voiceMode.error')}: ${event.message}`);
            stopVoice();
          }
        },
        onTranscript: ({ role, text, isFinal, completionId }) => {
          const uiRole: 'user' | 'agent' = role === 'user' ? 'user' : 'agent';
          setMessages((prev) => {
            // Primary dedup path: Nova Sonic gives the same `completionId`
            // for both the SPECULATIVE and FINAL content blocks of one reply.
            // Merge by (role, completionId). Distinct utterances get distinct
            // completionIds and render as separate bubbles.
            if (completionId) {
              const idx = prev.findIndex((m) => m.completionId === completionId && m.role === uiRole);
              if (idx >= 0) {
                const prevMsg = prev[idx];
                // Never regress: once a FINAL (pending=false) has landed for
                // this completion, ignore any later SPECULATIVE that Nova
                // might still emit (shouldn't happen per the protocol, but
                // defend against out-of-order frames).
                if (!prevMsg.pending && !isFinal) {
                  return prev;
                }
                const next = prev.slice();
                next[idx] = {
                  ...prevMsg,
                  content: text,
                  timestamp: new Date(),
                  pending: !isFinal,
                };
                return next;
              }
              // First time seeing this completionId — finalize older pending
              // bubbles from this role so stale SPECULATIVE fragments can't
              // be retargeted by later no-id events.
              const next = prev.map((m) =>
                m.role === uiRole && m.pending ? { ...m, pending: false } : m,
              );
              next.push({
                id: generateId(),
                role: uiRole,
                content: text,
                timestamp: new Date(),
                pending: !isFinal,
                completionId,
              });
              return next;
            }

            // Fallback (older agent builds that don't stamp completionId):
            // prefix-match against the most-recent same-role bubble.
            // Keeps the reducer backwards-compatible during rollout.
            for (let i = prev.length - 1; i >= 0; i--) {
              const m = prev[i];
              if (m.role !== uiRole) continue;
              if (m.completionId) {
                // Adjacent bubble already has an id — this no-id event is
                // something else; stop scanning.
                break;
              }
              const isContinuation =
                text === m.content ||
                text.startsWith(m.content) ||
                m.content.startsWith(text);
              if (m.pending && isContinuation) {
                const next = prev.slice();
                next[i] = { ...m, content: text, timestamp: new Date(), pending: !isFinal };
                return next;
              }
              break;
            }
            const next = prev.map((m) =>
              m.role === uiRole && m.pending ? { ...m, pending: false } : m,
            );
            next.push({ id: generateId(), role: uiRole, content: text, timestamp: new Date(), pending: !isFinal });
            return next;
          });
        },
      });
      voiceClientRef.current = client;
      await client.start();
      setVoiceActive(true);
    } catch (e: any) {
      if (e?.name === 'NotAllowedError') {
        setError(t('chat.voiceMode.micDenied'));
      } else {
        setError(`${t('chat.voiceMode.error')}: ${e?.message || e}`);
      }
      stopVoice();
    }
  }, [t, stopVoice]);

  useEffect(() => {
    // Ensure we release the mic / close the socket on unmount.
    return () => stopVoice();
  }, [stopVoice]);

  useEffect(() => {
    // Revoke any lingering blob URLs on unmount — both the live thumbnail
    // strip and every image URL captured on past messages.
    return () => {
      attachedImages.forEach((x) => URL.revokeObjectURL(x.previewUrl));
      messages.forEach((m) => m.imageUrls?.forEach((u) => URL.revokeObjectURL(u)));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The user turn that prompted a given agent reply, so a vote records WHAT was
  // asked. Without it the dashboard can show a 👎 rate but nothing about what
  // draws one, which is the only actionable half.
  const promptFor = useCallback((agentMsgId: string): string => {
    const idx = messages.findIndex((m) => m.id === agentMsgId);
    for (let i = idx - 1; i >= 0; i -= 1) {
      if (messages[i].role === 'user') return messages[i].content;
    }
    return '';
  }, [messages]);

  const postVote = useCallback(async (
    msg: ChatMessage,
    vote: 'up' | 'down',
    reason?: string,
  ) => {
    if (!browserUserId) return;
    await submitFeedback({
      userId: browserUserId,
      vote,
      turnId: msg.id,
      sessionId: browserAgentSessionId || undefined,
      reason,
      turnPrompt: promptFor(msg.id),
      // The delegation trace this turn already collected. Absent on turns that
      // did not stream (images, A/B), which the backend buckets explicitly
      // rather than dropping.
      agentDim: msg.trace,
    });
  }, [browserUserId, browserAgentSessionId, promptFor]);

  const sendVote = useCallback(async (msg: ChatMessage, vote: 'up' | 'down') => {
    // Optimistic: the arrow lights immediately. Reverted below if the call
    // fails, because a highlighted arrow whose vote never landed is a lie the
    // user has no way to detect.
    const previous = votes[msg.id];
    setVotes((v) => ({ ...v, [msg.id]: vote }));
    if (vote === 'down') {
      setReasonFor(msg.id);
      setReasonText('');
    } else if (reasonFor === msg.id) {
      setReasonFor('');
    }
    try {
      await postVote(msg, vote);
    } catch (e) {
      console.warn('feedback failed', e);
      setVotes((v) => {
        const next = { ...v };
        if (previous) next[msg.id] = previous;
        else delete next[msg.id];
        return next;
      });
      setReasonFor('');
      setError(t('chat.feedback.failed'));
    }
  }, [votes, reasonFor, postVote, t]);

  const sendReason = useCallback(async (msg: ChatMessage) => {
    const reason = reasonText.trim();
    setReasonFor('');
    setReasonText('');
    if (!reason) return;
    try {
      // Re-posts the same turn with the reason attached. The sort key embeds the
      // turn id, so this overwrites rather than counting a second 👎.
      await postVote(msg, 'down', reason);
    } catch (e) {
      console.warn('feedback reason failed', e);
      setError(t('chat.feedback.failed'));
    }
  }, [reasonText, postVote, t]);

  const toggleVoice = () => {
    if (voiceActive) stopVoice();
    else startVoice();
  };

  // Welcome-screen chips, from the SAME shared library as the drawer and the
  // simulated users' scripts. They used to be 40 hardcoded i18n keys that covered
  // only the orchestrator's own tools — nothing exercised any of the eight
  // specialists, so the welcome screen advertised a fraction of the system.
  const chips = welcomeChips(language === 'zh');

  // The shell's height is set imperatively by the fit() effect above — see it for
  // why CSS alone cannot bound it. `minHeight: 0` on every link of the chain is
  // what lets the inner scroller shrink below its content instead of pushing the
  // page taller: a flex item's default `min-height: auto` refuses to.
  return (
    <div ref={shellRef} style={{ display: 'flex', flexDirection: 'row', height: '100%', minHeight: 0 }}>
    <div className="chat-container" style={{ flex: 1, minWidth: 0, minHeight: 0 }}>
      {error && (
        <Box padding="s">
          <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>
        </Box>
      )}
      {voiceActive && voiceStatus && (
        <Box padding="s">
          <Alert type="info">🎤 {voiceStatus}</Alert>
        </Box>
      )}

      <div className="messages-area">
        {messages.length === 0 && (
          <Box textAlign="center" padding="xxxl">
            <SpaceBetween size="l" direction="vertical">
              <div>
                <Header variant="h2">{t('chat.welcome')}</Header>
                <Box color="text-body-secondary" padding={{ top: 'xs' }}>
                  {t('chat.subtitle')}
                </Box>
              </div>
              <SpaceBetween size="s" direction="vertical">
                <div className="welcome-chips">
                  {chips.map((c) => (
                    <Button key={c.label} onClick={() => setInputValue(c.label)}>
                      {c.label}
                    </Button>
                  ))}
                </div>
                {/* One chip per capability group, then a pointer at the full
                    library. The chips alone cannot show 60+ examples, and the
                    drawer is the part that stays reachable after the first
                    message. */}
                <Button iconName="suggestions" onClick={() => setExamplesOpen(true)}>
                  {t('examples.openAll')}
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          </Box>
        )}

        {messages.map((msg, idx) => (
          <React.Fragment key={msg.id}>
            {/* The boundary between restored history and this login's turns.
                Without it the user cannot tell which of these they just said —
                and a transcript that silently mixes the two invites "I never
                asked that". Sits BETWEEN the two blocks rather than marking every
                restored bubble, which would tint half the window. */}
            {historyCount > 0 && idx === historyCount && (
              <div className="history-divider">
                <span>{t('chat.historyBoundary')}</span>
              </div>
            )}
            {historyCount > 0 && idx === 0 && (
              <div className="history-divider history-divider-top">
                <span>
                  {historyNote
                    ? t('chat.historyRestoredMulti')
                        .replace('{turns}', String(historyCount))
                        .replace('{sessions}', historyNote)
                    : t('chat.historyRestored').replace('{turns}', String(historyCount))}
                </span>
              </div>
            )}
          <div
            className={`message-row ${msg.role === 'user' ? 'message-row-user' : 'message-row-agent'}`}
          >
            <div className={`message-bubble ${msg.role === 'user' ? 'bubble-user' : 'bubble-agent'}`}>
              {msg.imageUrls && msg.imageUrls.length > 0 && (
                <div className="message-images">
                  {msg.imageUrls.map((u, i) => (
                    <img key={i} src={u} alt="" className="message-image-thumb" />
                  ))}
                </div>
              )}
              {msg.content && (
                msg.role === 'agent' ? (
                  <div className="message-text message-markdown">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        a: ({ node: _node, ...props }) => (
                          <a {...props} target="_blank" rel="noopener noreferrer" />
                        ),
                      }}
                    >
                      {msg.content}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <div className="message-text">{msg.content}</div>
                )
              )}
              {/*
                Delegation trace (spec 5 S6). Which tools this turn actually used,
                in order — so a developer can see that a security question really
                did reach the specialist rather than being answered from the
                orchestrator's own knowledge. That distinction is invisible in the
                reply text: a confident wrong answer reads exactly like a delegated
                one, which is why `scripts/probe-routing.py` has to read spans.

                Collapsed by default; built from the progress events the turn
                already streamed, so it costs nothing and appears immediately.
              */}
              {msg.trace && msg.trace.length > 0 && (
                <details className="message-trace">
                  <summary>
                    {t('chat.trace') || 'how this was answered'} ({msg.trace.length})
                  </summary>
                  <ol>
                    {msg.trace.map((label, i) => (
                      <li key={`${msg.id}-trace-${i}`}>{label}</li>
                    ))}
                  </ol>
                </details>
              )}
              {/*
                Thumbs up/down. Only on agent turns, and only on settled ones —
                voting on a half-streamed reply would record an opinion about
                something the user has not finished reading.

                Before this control the Overview dashboard's satisfaction card was
                hardcoded mock data, because the only feedback path wrote JSON
                files into the runtime's workspace, inspectable one at a time and
                never aggregatable.
              */}
              {msg.role === 'agent' && !msg.pending && (
                <div className="message-feedback">
                  <Button
                    variant="inline-icon"
                    iconName={votes[msg.id] === 'up' ? 'thumbs-up-filled' : 'thumbs-up'}
                    ariaLabel={t('chat.feedback.up')}
                    onClick={() => void sendVote(msg, 'up')}
                  />
                  <Button
                    variant="inline-icon"
                    iconName={votes[msg.id] === 'down' ? 'thumbs-down-filled' : 'thumbs-down'}
                    ariaLabel={t('chat.feedback.down')}
                    onClick={() => void sendVote(msg, 'down')}
                  />
                  {votes[msg.id] && (
                    <Box variant="small" color="text-body-secondary" display="inline">
                      {t('chat.feedback.thanks')}
                    </Box>
                  )}
                </div>
              )}
              {/* The reason box appears only after a 👎, because an optional
                  "why" asked up front is a form; asked after a complaint it is a
                  follow-up question. The vote is already recorded either way, so
                  skipping this costs nothing. */}
              {reasonFor === msg.id && (
                <div className="message-reason">
                  <Textarea
                    value={reasonText}
                    onChange={({ detail }) => setReasonText(detail.value)}
                    placeholder={t('chat.feedback.reasonPlaceholder')}
                    rows={2}
                  />
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="primary" onClick={() => void sendReason(msg)}>
                      {t('chat.feedback.reasonSubmit')}
                    </Button>
                    <Button variant="link" onClick={() => { setReasonFor(''); setReasonText(''); }}>
                      {t('chat.feedback.reasonSkip')}
                    </Button>
                  </SpaceBetween>
                </div>
              )}
              <div className="message-time">{formatTime(msg.timestamp)}</div>
            </div>
          </div>
          </React.Fragment>
        ))}

        {isTyping && (
          <div className="message-row message-row-agent">
            <div className="message-bubble bubble-agent">
              <StatusIndicator type="loading">
                {progressTool
                  ? `${t('chat.consulting') || 'asking the'} ${progressTool}…`
                  : t('chat.typing') || 'thinking…'}
              </StatusIndicator>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      <Container>
        <SpaceBetween size="xs">
          {attachedImages.length > 0 && (
            <div className="image-strip">
              {attachedImages.map((img) => (
                <div key={img.id} className="image-strip-item">
                  <img src={img.previewUrl} alt="" title={img.file.name} />
                  <button
                    type="button"
                    className="image-strip-remove"
                    onClick={() => removeImage(img.id)}
                    aria-label={t('chat.image.remove')}
                    title={t('chat.image.remove')}
                  >×</button>
                </div>
              ))}
            </div>
          )}
          {imageErrors.length > 0 && (
            <SpaceBetween size="xxs">
              {imageErrors.map((e, i) => (
                <Alert key={i} type="error">{e}</Alert>
              ))}
            </SpaceBetween>
          )}
          <div className="input-row">
            <div className="input-row-buttons">
              <Button
                iconName="microphone"
                variant={voiceActive ? 'primary' : 'normal'}
                onClick={toggleVoice}
                ariaLabel={voiceActive ? t('chat.voiceMode.exit') : t('chat.voiceMode.enter')}
              />
              <Button
                iconName="upload"
                onClick={() => fileInputRef.current?.click()}
                disabled={voiceActive || attachedImages.length >= MAX_IMAGES_PER_MESSAGE}
                ariaLabel={t('chat.attachImage')}
              />
              {/* The example library, reachable mid-conversation. This is the
                  control the old welcome-only chips lacked: once the user had
                  said anything, there was no way back to the list of what the
                  agent can do. */}
              <Button
                iconName="suggestions"
                onClick={() => setExamplesOpen((v) => !v)}
                ariaLabel={t('examples.title')}
              />
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept={IMAGE_MIME_ALLOWLIST.join(',')}
              multiple
              style={{ display: 'none' }}
              onChange={(e) => {
                if (e.target.files) addImages(e.target.files);
              }}
            />
            <div className="input-row-textarea">
              <Textarea
                value={inputValue}
                onChange={({ detail }) => setInputValue(detail.value)}
                onKeyDown={({ detail }) => {
                  // Cloudscape Textarea's onKeyDown fires with detail = { key, ctrlKey, ... }.
                  // Enter (without shift) submits; shift-enter adds a newline like the old textarea.
                  if (detail.key === 'Enter' && !detail.shiftKey) {
                    // Need to prevent the default keystroke from inserting a newline.
                    // Cloudscape doesn't expose the raw event, so defer to a native handler below.
                  }
                }}
                placeholder={t('chat.placeholder')}
                rows={2}
                disabled={voiceActive}
              />
            </div>
            <Button
              variant="primary"
              iconName="send"
              onClick={sendMessage}
              disabled={(!inputValue.trim() && attachedImages.length === 0) || voiceActive}
              ariaLabel="Send message"
            />
          </div>
        </SpaceBetween>
      </Container>
      {/* Preserve Enter-to-send behavior using a capture-phase keydown on the
          textarea element; Cloudscape's onKeyDown doesn't let us preventDefault. */}
      <InputKeyBindings textareaContainerSelector=".input-row-textarea textarea" onSubmit={sendMessage} disabled={voiceActive} />
    </div>
      {examplesOpen && (
        <PromptExamples
          onPick={(prompt) => {
            // Staged, not sent. A presenter wants a beat to narrate what the
            // example demonstrates; an explorer wants to edit it first.
            setInputValue(prompt);
            setExamplesOpen(false);
          }}
          onClose={() => setExamplesOpen(false)}
        />
      )}
      <BrowserPanel
        session={browserSession}
        codeSession={codeSession}
        agentSessionId={browserAgentSessionId}
        expanded={browserPanelExpanded}
        activeTab={browserPanelTab}
        onExpand={(tab) => { setBrowserPanelTab(tab); setBrowserPanelExpanded(true); }}
        onCollapse={() => setBrowserPanelExpanded(false)}
      />
    </div>
  );
};

// Captures Enter keypresses on the Cloudscape Textarea to trigger send,
// without breaking shift+Enter for newlines. Cloudscape's onKeyDown handler
// fires the callback but does not hand back the raw event, so we attach a
// DOM listener on the underlying textarea node.
const InputKeyBindings: React.FC<{
  textareaContainerSelector: string;
  onSubmit: () => void;
  disabled: boolean;
}> = ({ textareaContainerSelector, onSubmit, disabled }) => {
  useEffect(() => {
    const node = document.querySelector(textareaContainerSelector) as HTMLTextAreaElement | null;
    if (!node) return;
    const handler = (e: KeyboardEvent) => {
      if (disabled) return;
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        onSubmit();
      }
    };
    node.addEventListener('keydown', handler);
    return () => node.removeEventListener('keydown', handler);
  }, [textareaContainerSelector, onSubmit, disabled]);
  return null;
};

export default ChatInterface;
