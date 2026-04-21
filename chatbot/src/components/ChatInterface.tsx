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
  // Track the pending speculative transcript per role so we can replace it
  // in-place when the FINAL pass arrives (Nova Sonic emits both). Without
  // this, each spoken utterance shows up twice in the chat history.
  const pendingTranscriptRef = useRef<Record<'user' | 'agent', string | null>>({ user: null, agent: null });

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, isTyping, scrollToBottom]);

  // Warm up the AgentCore Runtime immediately after login so the first real
  // message doesn't pay a cold-start penalty. The agent short-circuits the
  // "__warmup__" prompt without invoking the LLM.
  useEffect(() => {
    if (warmupRef.current) return;
    warmupRef.current = true;
    (async () => {
      try {
        const config = getConfig();
        const token = await getIdToken();
        const creds = await getAwsCredentials();
        const decoded = jwtDecode<{ sub: string; email?: string; 'cognito:username'?: string }>(token);
        const userId = decoded.email || decoded['cognito:username'] || decoded.sub;
        const sessionId = `user-session-${decoded.sub}`;
        await signedInvocationsFetch({
          agentRuntimeArn: config.agentRuntimeArn,
          region: config.region,
          credentials: creds,
          sessionId,
          body: { prompt: '__warmup__', userId },
          extraHeaders: { [CUSTOM_AUTH_HEADER]: token },
        });
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
    pendingTranscriptRef.current = { user: null, agent: null };
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

      // The AuthToken flows as a query-string allowlisted custom header
      // per AgentCore WS docs so the agent can forward it to the MCP gateway.
      const wsUrl = await presignWsUrl({
        agentRuntimeArn: config.agentRuntimeArn,
        region: config.region,
        credentials: creds,
        sessionId,
        expiresSeconds: 300,
        extraQueryParams: {
          [CUSTOM_AUTH_HEADER]: token,
        },
      });

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
        onTranscript: ({ role, text, isFinal }) => {
          const uiRole: 'user' | 'agent' = role === 'user' ? 'user' : 'agent';
          const pendingId = pendingTranscriptRef.current[uiRole];
          setMessages((prev) => {
            if (pendingId) {
              // Replace the speculative bubble with the latest (final or updated speculative) text.
              return prev.map((m) => (m.id === pendingId ? { ...m, content: text, timestamp: new Date() } : m));
            }
            const id = generateId();
            pendingTranscriptRef.current[uiRole] = id;
            return [...prev, { id, role: uiRole, content: text, timestamp: new Date() }];
          });
          if (isFinal) {
            pendingTranscriptRef.current[uiRole] = null;
          }
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
