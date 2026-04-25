import React, { useState, useEffect } from 'react';
import { signIn, signUp, confirmSignUp, AuthTokens } from './CognitoAuth';
import { useI18n } from '../i18n';
import { getConfig } from '../config';

interface LoginPageProps {
  onAuthenticated: (tokens: AuthTokens) => void;
}

type FormMode = 'signIn' | 'signUp' | 'confirm';

function getInitialUsername(): string {
  if (typeof window === 'undefined') return '';
  try {
    const params = new URLSearchParams(window.location.search);
    return params.get('username') || params.get('email') || '';
  } catch {
    return '';
  }
}

const LoginPage: React.FC<LoginPageProps> = ({ onAuthenticated }) => {
  const [mode, setMode] = useState<FormMode>('signIn');
  const [username, setUsername] = useState(getInitialUsername);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmCode, setConfirmCode] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const { t, language, setLanguage } = useI18n();

  // Preconnect to the AgentCore Runtime endpoint while the user types their
  // password so the TLS handshake for the post-login warmup + the voice WS
  // presign is already paid by the time we need it.
  useEffect(() => {
    try {
      const region = getConfig().region;
      const href = `https://bedrock-agentcore.${region}.amazonaws.com`;
      const link = document.createElement('link');
      link.rel = 'preconnect';
      link.href = href;
      link.crossOrigin = 'anonymous';
      document.head.appendChild(link);
      return () => {
        document.head.removeChild(link);
      };
    } catch {
      // Non-critical hint; ignore failures.
    }
  }, []);

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const tokens = await signIn(username, password);
      onAuthenticated(tokens);
    } catch (err: any) {
      setError(err.message || t('login.signInFailed'));
    } finally {
      setLoading(false);
    }
  };

  const handleSignUp = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await signUp(username, password, email);
      setMode('confirm');
    } catch (err: any) {
      setError(err.message || t('login.signUpFailed'));
    } finally {
      setLoading(false);
    }
  };

  const handleConfirm = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await confirmSignUp(username, confirmCode);
      const tokens = await signIn(username, password);
      onAuthenticated(tokens);
    } catch (err: any) {
      setError(err.message || t('login.confirmFailed'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        <div style={styles.langRow}>
          <button
            style={styles.langButton}
            onClick={() => setLanguage(language === 'en' ? 'zh' : 'en')}
          >
            {language === 'en' ? '中文' : 'EN'}
          </button>
        </div>
        <div style={styles.logoArea}>
          <div style={styles.logoIcon}>
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#4a9eff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
              <polyline points="9 22 9 12 15 12 15 22" />
            </svg>
          </div>
          <h1 style={styles.title}>{t('login.title')}</h1>
          <p style={styles.subtitle}>
            {mode === 'signIn' && t('login.signIn.subtitle')}
            {mode === 'signUp' && t('login.signUp.subtitle')}
            {mode === 'confirm' && t('login.confirm.subtitle')}
          </p>
        </div>

        {error && <div style={styles.error}>{error}</div>}

        {mode === 'signIn' && (
          <form onSubmit={handleSignIn} style={styles.form}>
            <div style={styles.field}>
              <label style={styles.label}>{t('login.username')}</label>
              <input
                style={styles.input}
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder={t('login.enterUsername')}
                required
                autoComplete="username"
              />
            </div>
            <div style={styles.field}>
              <label style={styles.label}>{t('login.password')}</label>
              <input
                style={styles.input}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t('login.enterPassword')}
                required
                autoComplete="current-password"
              />
            </div>
            <button style={styles.button} type="submit" disabled={loading}>
              {loading ? t('login.signingIn') : t('login.signIn')}
            </button>
            <p style={styles.switchText}>
              {t('login.noAccount')}{' '}
              <span style={styles.link} onClick={() => { setMode('signUp'); setError(''); }}>
                {t('login.signUp')}
              </span>
            </p>
          </form>
        )}

        {mode === 'signUp' && (
          <form onSubmit={handleSignUp} style={styles.form}>
            <div style={styles.field}>
              <label style={styles.label}>{t('login.username')}</label>
              <input
                style={styles.input}
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder={t('login.chooseUsername')}
                required
                autoComplete="username"
              />
            </div>
            <div style={styles.field}>
              <label style={styles.label}>{t('login.email')}</label>
              <input
                style={styles.input}
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder={t('login.enterEmail')}
                required
                autoComplete="email"
              />
            </div>
            <div style={styles.field}>
              <label style={styles.label}>{t('login.password')}</label>
              <input
                style={styles.input}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t('login.choosePassword')}
                required
                autoComplete="new-password"
              />
            </div>
            <button style={styles.button} type="submit" disabled={loading}>
              {loading ? t('login.creatingAccount') : t('login.signUp')}
            </button>
            <p style={styles.switchText}>
              {t('login.hasAccount')}{' '}
              <span style={styles.link} onClick={() => { setMode('signIn'); setError(''); }}>
                {t('login.signIn')}
              </span>
            </p>
          </form>
        )}

        {mode === 'confirm' && (
          <form onSubmit={handleConfirm} style={styles.form}>
            <p style={styles.confirmInfo}>
              {t('login.verificationSent')}
            </p>
            <div style={styles.field}>
              <label style={styles.label}>{t('login.confirmCode')}</label>
              <input
                style={styles.input}
                type="text"
                value={confirmCode}
                onChange={(e) => setConfirmCode(e.target.value)}
                placeholder={t('login.enterCode')}
                required
                autoComplete="one-time-code"
              />
            </div>
            <button style={styles.button} type="submit" disabled={loading}>
              {loading ? t('login.verifying') : t('login.confirmAccount')}
            </button>
            <p style={styles.switchText}>
              <span style={styles.link} onClick={() => { setMode('signIn'); setError(''); }}>
                {t('login.backToSignIn')}
              </span>
            </p>
          </form>
        )}
      </div>
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: '100vh',
    background: 'linear-gradient(135deg, #0a0a0f 0%, #111128 50%, #0a0a0f 100%)',
    padding: '20px',
  },
  card: {
    width: '100%',
    maxWidth: '420px',
    background: 'rgba(20, 20, 35, 0.95)',
    borderRadius: '16px',
    padding: '40px',
    border: '1px solid rgba(74, 158, 255, 0.15)',
    boxShadow: '0 20px 60px rgba(0, 0, 0, 0.5), 0 0 40px rgba(74, 158, 255, 0.05)',
  },
  langRow: {
    display: 'flex',
    justifyContent: 'flex-end',
    marginBottom: '8px',
    marginTop: '-12px',
  },
  langButton: {
    padding: '4px 12px',
    borderRadius: '6px',
    border: '1px solid rgba(255, 255, 255, 0.15)',
    background: 'rgba(255, 255, 255, 0.05)',
    color: '#aaaacc',
    fontSize: '12px',
    fontWeight: 500,
    cursor: 'pointer',
  },
  logoArea: {
    textAlign: 'center' as const,
    marginBottom: '32px',
  },
  logoIcon: {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: '64px',
    height: '64px',
    borderRadius: '16px',
    background: 'rgba(74, 158, 255, 0.1)',
    marginBottom: '16px',
  },
  title: {
    fontSize: '24px',
    fontWeight: 700,
    color: '#ffffff',
    margin: '0 0 8px 0',
  },
  subtitle: {
    fontSize: '14px',
    color: '#8888aa',
    margin: 0,
  },
  form: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '20px',
  },
  field: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '6px',
  },
  label: {
    fontSize: '13px',
    fontWeight: 500,
    color: '#aaaacc',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.5px',
  },
  input: {
    padding: '12px 16px',
    borderRadius: '10px',
    border: '1px solid rgba(255, 255, 255, 0.1)',
    background: 'rgba(255, 255, 255, 0.05)',
    color: '#e0e0e0',
    fontSize: '15px',
    outline: 'none',
    transition: 'border-color 0.2s, box-shadow 0.2s',
  },
  button: {
    padding: '14px',
    borderRadius: '10px',
    border: 'none',
    background: 'linear-gradient(135deg, #4a9eff, #2d7ed9)',
    color: '#ffffff',
    fontSize: '15px',
    fontWeight: 600,
    cursor: 'pointer',
    transition: 'opacity 0.2s, transform 0.1s',
    marginTop: '4px',
  },
  error: {
    padding: '12px 16px',
    borderRadius: '10px',
    background: 'rgba(255, 60, 60, 0.1)',
    border: '1px solid rgba(255, 60, 60, 0.3)',
    color: '#ff6b6b',
    fontSize: '13px',
    marginBottom: '8px',
  },
  switchText: {
    textAlign: 'center' as const,
    fontSize: '14px',
    color: '#8888aa',
    margin: 0,
  },
  link: {
    color: '#4a9eff',
    cursor: 'pointer',
    fontWeight: 500,
  },
  confirmInfo: {
    fontSize: '14px',
    color: '#aaaacc',
    textAlign: 'center' as const,
    lineHeight: 1.5,
    margin: 0,
  },
};

export default LoginPage;
