import React, { useState } from 'react';
import { signIn, signUp, confirmSignUp, AuthTokens } from './CognitoAuth';

interface LoginPageProps {
  onAuthenticated: (tokens: AuthTokens) => void;
}

type FormMode = 'signIn' | 'signUp' | 'confirm';

const LoginPage: React.FC<LoginPageProps> = ({ onAuthenticated }) => {
  const [mode, setMode] = useState<FormMode>('signIn');
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmCode, setConfirmCode] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const tokens = await signIn(username, password);
      onAuthenticated(tokens);
    } catch (err: any) {
      setError(err.message || 'Sign in failed');
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
      setError(err.message || 'Sign up failed');
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
      setError(err.message || 'Confirmation failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        <div style={styles.logoArea}>
          <div style={styles.logoIcon}>
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#4a9eff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
              <polyline points="9 22 9 12 15 12 15 22" />
            </svg>
          </div>
          <h1 style={styles.title}>Smart Home Assistant</h1>
          <p style={styles.subtitle}>
            {mode === 'signIn' && 'Sign in to your account'}
            {mode === 'signUp' && 'Create a new account'}
            {mode === 'confirm' && 'Verify your email'}
          </p>
        </div>

        {error && <div style={styles.error}>{error}</div>}

        {mode === 'signIn' && (
          <form onSubmit={handleSignIn} style={styles.form}>
            <div style={styles.field}>
              <label style={styles.label}>Username</label>
              <input
                style={styles.input}
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="Enter your username"
                required
                autoComplete="username"
              />
            </div>
            <div style={styles.field}>
              <label style={styles.label}>Password</label>
              <input
                style={styles.input}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Enter your password"
                required
                autoComplete="current-password"
              />
            </div>
            <button style={styles.button} type="submit" disabled={loading}>
              {loading ? 'Signing in...' : 'Sign In'}
            </button>
            <p style={styles.switchText}>
              Don't have an account?{' '}
              <span style={styles.link} onClick={() => { setMode('signUp'); setError(''); }}>
                Sign Up
              </span>
            </p>
          </form>
        )}

        {mode === 'signUp' && (
          <form onSubmit={handleSignUp} style={styles.form}>
            <div style={styles.field}>
              <label style={styles.label}>Username</label>
              <input
                style={styles.input}
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="Choose a username"
                required
                autoComplete="username"
              />
            </div>
            <div style={styles.field}>
              <label style={styles.label}>Email</label>
              <input
                style={styles.input}
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="Enter your email"
                required
                autoComplete="email"
              />
            </div>
            <div style={styles.field}>
              <label style={styles.label}>Password</label>
              <input
                style={styles.input}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Choose a password"
                required
                autoComplete="new-password"
              />
            </div>
            <button style={styles.button} type="submit" disabled={loading}>
              {loading ? 'Creating account...' : 'Sign Up'}
            </button>
            <p style={styles.switchText}>
              Already have an account?{' '}
              <span style={styles.link} onClick={() => { setMode('signIn'); setError(''); }}>
                Sign In
              </span>
            </p>
          </form>
        )}

        {mode === 'confirm' && (
          <form onSubmit={handleConfirm} style={styles.form}>
            <p style={styles.confirmInfo}>
              A verification code has been sent to your email address.
            </p>
            <div style={styles.field}>
              <label style={styles.label}>Confirmation Code</label>
              <input
                style={styles.input}
                type="text"
                value={confirmCode}
                onChange={(e) => setConfirmCode(e.target.value)}
                placeholder="Enter the 6-digit code"
                required
                autoComplete="one-time-code"
              />
            </div>
            <button style={styles.button} type="submit" disabled={loading}>
              {loading ? 'Verifying...' : 'Confirm Account'}
            </button>
            <p style={styles.switchText}>
              <span style={styles.link} onClick={() => { setMode('signIn'); setError(''); }}>
                Back to Sign In
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
