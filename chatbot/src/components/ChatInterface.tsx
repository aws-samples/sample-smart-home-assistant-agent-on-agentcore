import React, { useState, useEffect, useRef, useCallback } from 'react';
import { jwtDecode } from 'jwt-decode';
import { getConfig } from '../config';
import { getIdToken, getAwsCredentials } from '../auth/CognitoAuth';
import { useI18n } from '../i18n';
import { VoiceClient } from '../voice/VoiceClient';
import { signedInvocationsFetch, presignWsUrl } from '../voice/sigv4';

const CUSTOM_AUTH_HEADER = 'X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken';

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
  const { t } = useI18n();

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
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

  const sendMessage = useCallback(async () => {
    const text = inputValue.trim();
    if (!text) return;

    const userMessage: ChatMessage = {
      id: generateId(),
      role: 'user',
      content: text,
      timestamp: new Date(),
    };

    setMessages((prev) => [...prev, userMessage]);
    setInputValue('');
    setIsTyping(true);
    setError('');

    try {
      const config = getConfig();
      const token = await getIdToken();
      const creds = await getAwsCredentials();
      const decoded = jwtDecode<{ sub: string; email?: string; 'cognito:username'?: string }>(token);
      const userId = decoded.email || decoded['cognito:username'] || decoded.sub;
      const sessionId = `user-session-${decoded.sub}`;

      const response = await signedInvocationsFetch({
        agentRuntimeArn: config.agentRuntimeArn,
        region: config.region,
        credentials: creds,
        sessionId,
        body: { prompt: text, userId },
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
  }, [inputValue, t]);

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

  const toggleVoice = () => {
    if (voiceActive) stopVoice();
    else startVoice();
  };

  return (
    <div className="chat-container">
      {error && <div className="connection-banner">{error}</div>}
      {voiceActive && voiceStatus && (
        <div className="connection-banner" style={{ background: '#2d4a2d' }}>🎤 {voiceStatus}</div>
      )}

      <div className="messages-area">
        {messages.length === 0 && (
          <div className="empty-state">
            <div className="empty-icon">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#4a9eff" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
                <polyline points="9 22 9 12 15 12 15 22" />
              </svg>
            </div>
            <h3 className="empty-title">{t('chat.welcome')}</h3>
            <p className="empty-subtitle">
              {t('chat.subtitle')}
            </p>
            <div className="suggestion-chips">
              <button className="chip" onClick={() => setInputValue(t('chat.chip.checkDevices.prompt'))}>
                {t('chat.chip.checkDevices')}
              </button>
              <button className="chip" onClick={() => setInputValue(t('chat.chip.turnOnAll.prompt'))}>
                {t('chat.chip.turnOnAll')}
              </button>
              <button className="chip" onClick={() => setInputValue(t('chat.chip.changeLed.prompt'))}>
                {t('chat.chip.changeLed')}
              </button>
              <button className="chip" onClick={() => setInputValue(t('chat.chip.cookRice.prompt'))}>
                {t('chat.chip.cookRice')}
              </button>
              <button className="chip" onClick={() => setInputValue(t('chat.chip.turnOnFan.prompt'))}>
                {t('chat.chip.turnOnFan')}
              </button>
              <button className="chip" onClick={() => setInputValue(t('chat.chip.preheatOven.prompt'))}>
                {t('chat.chip.preheatOven')}
              </button>
            </div>
          </div>
        )}

        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`message-row ${msg.role === 'user' ? 'message-row-user' : 'message-row-agent'}`}
          >
            {msg.role === 'agent' && (
              <div className="avatar avatar-agent">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
                  <polyline points="9 22 9 12 15 12 15 22" />
                </svg>
              </div>
            )}
            <div className={`message-bubble ${msg.role === 'user' ? 'bubble-user' : 'bubble-agent'}`}>
              <div className="message-text">{msg.content}</div>
              <div className="message-time">{formatTime(msg.timestamp)}</div>
            </div>
          </div>
        ))}

        {isTyping && (
          <div className="message-row message-row-agent">
            <div className="avatar avatar-agent">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
                <polyline points="9 22 9 12 15 12 15 22" />
              </svg>
            </div>
            <div className="typing-indicator">
              <span className="typing-dot" />
              <span className="typing-dot" />
              <span className="typing-dot" />
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      <div className="input-area">
        <div className="input-wrapper">
          <button
            className={`send-button ${voiceActive ? 'send-active' : ''}`}
            onClick={toggleVoice}
            aria-label={voiceActive ? t('chat.voiceMode.exit') : t('chat.voiceMode.enter')}
            title={voiceActive ? t('chat.voiceMode.exit') : t('chat.voiceMode.enter')}
            style={{ marginRight: 8 }}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill={voiceActive ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
              <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
              <line x1="12" y1="19" x2="12" y2="23" />
              <line x1="8" y1="23" x2="16" y2="23" />
            </svg>
          </button>
          <textarea
            ref={inputRef}
            className="message-input"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t('chat.placeholder')}
            rows={1}
            disabled={voiceActive}
          />
          <button
            className={`send-button ${inputValue.trim() ? 'send-active' : ''}`}
            onClick={sendMessage}
            disabled={!inputValue.trim() || voiceActive}
            aria-label="Send message"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="22" y1="2" x2="11" y2="13" />
              <polygon points="22 2 15 22 11 13 2 9 22 2" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
};

export default ChatInterface;
