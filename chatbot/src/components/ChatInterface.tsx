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
import { signedInvocationsFetch, presignWsUrl } from '../voice/sigv4';
import BrowserPanel from './BrowserPanel';
import { fetchActiveBrowserSession, BrowserSessionInfo } from '../api/browserSessions';

const CUSTOM_AUTH_HEADER = 'X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken';

const IMAGE_MIME_ALLOWLIST = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];
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

const ChatInterface: React.FC = () => {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [isTyping, setIsTyping] = useState(false);
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
  const [browserPanelTab, setBrowserPanelTab] = useState<'live' | 'files'>('live');
  // Timestamp of the most recent sendMessage — used by the polling effect
  // to drop any DDB rows from previous runs (startedAt < this) so the
  // panel doesn't render a dead session while waiting for the new
  // tool call to write its `running` row.
  const sendStartAtRef = useRef<number>(0);
  const { t } = useI18n();

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const voiceClientRef = useRef<VoiceClient | null>(null);
  const warmupRef = useRef(false);
  // Pre-signed voice WS URL cached from the post-login warmup. Saves the
  // SigV4 presign + Identity Pool creds fetch (~200-400ms) on the first
  // voice button tap. AgentCore presigned URLs are valid for 5 min; we
  // expire ours at 4 min to be safe.
  const presignedWsRef = useRef<{ url: string; expiresAt: number } | null>(null);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, isTyping, scrollToBottom]);

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
        const sessionId = `user-session-${decoded.sub}`;

        const targets = [config.agentRuntimeArn];
        // Voice runtime is a separate ARN; fall back to text ARN if the deploy
        // hasn't been run post-split yet (transition safety).
        if (config.voiceAgentRuntimeArn && config.voiceAgentRuntimeArn !== config.agentRuntimeArn) {
          targets.push(config.voiceAgentRuntimeArn);
        }

        await Promise.allSettled(targets.map((arn) =>
          signedInvocationsFetch({
            agentRuntimeArn: arn,
            region: config.region,
            credentials: creds,
            sessionId,
            body: { prompt: '__warmup__', userId },
            extraHeaders: { [CUSTOM_AUTH_HEADER]: token },
          })
        ));

        // Pre-presign the voice WS URL with the same creds we already have
        // in scope. The presigned URL is signed for 5 min; cache with a 4-min
        // TTL so stale-but-unexpired URLs never hit the runtime. startVoice
        // will re-presign if the cached entry has aged past the TTL.
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
        setBrowserAgentSessionId(`user-session-${decoded.sub}`);
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
    sendStartAtRef.current = Date.now();

    try {
      const config = getConfig();
      const token = await getIdToken();
      const creds = await getAwsCredentials();
      const decoded = jwtDecode<{ sub: string; email?: string; 'cognito:username'?: string }>(token);
      const userId = decoded.email || decoded['cognito:username'] || decoded.sub;
      const sessionId = `user-session-${decoded.sub}`;
      setBrowserUserId(userId);
      setBrowserAgentSessionId(sessionId);

      let images: Array<{ mediaType: string; data: string }> | undefined;
      if (snapshotFiles.length > 0) {
        const encoded = await Promise.all(
          snapshotFiles.map(async (f) => ({ mediaType: f.type, data: await fileToBase64(f) })),
        );
        images = encoded;
      }

      const body: Record<string, unknown> = { prompt: text, userId };
      if (images) body.images = images;

      const response = await signedInvocationsFetch({
        agentRuntimeArn: config.agentRuntimeArn,
        region: config.region,
        credentials: creds,
        sessionId,
        body,
        extraHeaders: { [CUSTOM_AUTH_HEADER]: token },
      });

      if (!response.ok) {
        const errBody = await response.text();
        throw new Error(`Request failed (${response.status}): ${errBody}`);
      }

      const data = await response.json();
      const agentText = data.response || data.text || data.content || JSON.stringify(data);

      setMessages((prev) => [
        ...prev,
        {
          id: generateId(),
          role: 'agent',
          content: agentText,
          timestamp: new Date(),
        },
      ]);
    } catch (err: any) {
      setError(err.message || t('chat.sendFailed'));
    } finally {
      setIsTyping(false);
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
      const sessionId = `user-session-${decoded.sub}`;

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

  const toggleVoice = () => {
    if (voiceActive) stopVoice();
    else startVoice();
  };

  // Suggestion chips grouped by capability so the welcome screen
  // surfaces the agent's full surface area (device control, knowledge
  // base, weather, vision, live-web). Clicking a chip stages the prompt
  // in the input box; the user still has to hit Send.
  const suggestionGroups: Array<{ title: string; chips: Array<{ label: string; prompt: string }> }> = [
    {
      title: t('chat.group.devices'),
      chips: [
        { label: t('chat.chip.checkDevices'), prompt: t('chat.chip.checkDevices.prompt') },
        { label: t('chat.chip.turnOnAll'), prompt: t('chat.chip.turnOnAll.prompt') },
        { label: t('chat.chip.changeLed'), prompt: t('chat.chip.changeLed.prompt') },
        { label: t('chat.chip.cookRice'), prompt: t('chat.chip.cookRice.prompt') },
        { label: t('chat.chip.turnOnFan'), prompt: t('chat.chip.turnOnFan.prompt') },
        { label: t('chat.chip.preheatOven'), prompt: t('chat.chip.preheatOven.prompt') },
      ],
    },
    {
      title: t('chat.group.knowledge'),
      chips: [
        { label: t('chat.chip.kb.ledManual'), prompt: t('chat.chip.kb.ledManual.prompt') },
        { label: t('chat.chip.kb.ricePresets'), prompt: t('chat.chip.kb.ricePresets.prompt') },
        { label: t('chat.chip.kb.fanErrors'), prompt: t('chat.chip.kb.fanErrors.prompt') },
      ],
    },
    {
      title: t('chat.group.weather'),
      chips: [
        { label: t('chat.chip.weather.today'), prompt: t('chat.chip.weather.today.prompt') },
        { label: t('chat.chip.weather.beijing'), prompt: t('chat.chip.weather.beijing.prompt') },
      ],
    },
    {
      title: t('chat.group.browser'),
      chips: [
        { label: t('chat.chip.browser.example'), prompt: t('chat.chip.browser.example.prompt') },
        { label: t('chat.chip.browser.amazon'), prompt: t('chat.chip.browser.amazon.prompt') },
        { label: t('chat.chip.browser.wiki'), prompt: t('chat.chip.browser.wiki.prompt') },
        { label: t('chat.chip.browser.httpbin'), prompt: t('chat.chip.browser.httpbin.prompt') },
      ],
    },
    {
      title: t('chat.group.vision'),
      chips: [
        { label: t('chat.chip.vision.describe'), prompt: t('chat.chip.vision.describe.prompt') },
      ],
    },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'row', height: '100%' }}>
    <div className="chat-container" style={{ flex: 1, minWidth: 0 }}>
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
              <SpaceBetween size="m" direction="vertical">
                {suggestionGroups.map((g) => (
                  <SpaceBetween key={g.title} size="xs" direction="vertical">
                    <Box color="text-body-secondary" fontSize="body-s">{g.title}</Box>
                    <SpaceBetween direction="horizontal" size="xs">
                      {g.chips.map((c) => (
                        <Button key={c.label} onClick={() => setInputValue(c.prompt)}>
                          {c.label}
                        </Button>
                      ))}
                    </SpaceBetween>
                  </SpaceBetween>
                ))}
              </SpaceBetween>
            </SpaceBetween>
          </Box>
        )}

        {messages.map((msg) => (
          <div
            key={msg.id}
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
              <div className="message-time">{formatTime(msg.timestamp)}</div>
            </div>
          </div>
        ))}

        {isTyping && (
          <div className="message-row message-row-agent">
            <div className="message-bubble bubble-agent">
              <StatusIndicator type="loading">{t('chat.typing') || 'thinking…'}</StatusIndicator>
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
      <BrowserPanel
        session={browserSession}
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
