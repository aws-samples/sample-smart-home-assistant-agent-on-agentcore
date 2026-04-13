import React, { useState, useEffect, useRef, useCallback } from 'react';
import { jwtDecode } from 'jwt-decode';
import { getConfig } from '../config';
import { getIdToken } from '../auth/CognitoAuth';
import { useI18n } from '../i18n';

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
  const { t } = useI18n();

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, isTyping, scrollToBottom]);

  // Send via HTTP POST to AgentCore Runtime
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

      const url = `https://bedrock-agentcore.${config.region}.amazonaws.com/runtimes/${encodeURIComponent(config.agentRuntimeArn)}/invocations`;

      // Derive a stable session ID and user ID from the JWT
      const decoded = jwtDecode<{ sub: string; email?: string; 'cognito:username'?: string }>(token);
      const userId = decoded.email || decoded['cognito:username'] || decoded.sub;
      const sessionId = `user-session-${decoded.sub}`;

      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json',
          'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': sessionId,
        },
        body: JSON.stringify({ prompt: text, userId }),
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

  return (
    <div className="chat-container">
      {error && <div className="connection-banner">{error}</div>}

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
          <textarea
            ref={inputRef}
            className="message-input"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t('chat.placeholder')}
            rows={1}
          />
          <button
            className={`send-button ${inputValue.trim() ? 'send-active' : ''}`}
            onClick={sendMessage}
            disabled={!inputValue.trim()}
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
