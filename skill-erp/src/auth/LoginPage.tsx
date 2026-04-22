import React, { useState } from 'react';
import { signIn, signUp, confirmSignUp, AuthTokens } from './CognitoAuth';
import { useI18n } from '../i18n';

interface LoginPageProps {
  onAuthenticated: (tokens: AuthTokens) => void;
}

type Mode = 'signin' | 'signup' | 'confirm';

const LoginPage: React.FC<LoginPageProps> = ({ onAuthenticated }) => {
  const [mode, setMode] = useState<Mode>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const { t, language, setLanguage } = useI18n();

  const clearMessages = () => {
    setError('');
    setInfo('');
  };

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault();
    clearMessages();
    setIsLoading(true);
    try {
      const tokens = await signIn(email, password);
      onAuthenticated(tokens);
    } catch (err: any) {
      setError(err.message || t('login.signInFailed'));
    } finally {
      setIsLoading(false);
    }
  };

  const handleSignUp = async (e: React.FormEvent) => {
    e.preventDefault();
    clearMessages();
    setIsLoading(true);
    try {
      await signUp(email, password);
      setMode('confirm');
      setInfo(t('login.confirmSent'));
    } catch (err: any) {
      setError(err.message || t('login.signUpFailed'));
    } finally {
      setIsLoading(false);
    }
  };

  const handleConfirm = async (e: React.FormEvent) => {
    e.preventDefault();
    clearMessages();
    setIsLoading(true);
    try {
      await confirmSignUp(email, code);
      const tokens = await signIn(email, password);
      onAuthenticated(tokens);
    } catch (err: any) {
      setError(err.message || t('login.confirmFailed'));
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="login-container">
      <div className="login-card">
        <div className="login-lang-row">
          <button
            className="lang-switch"
            onClick={() => setLanguage(language === 'en' ? 'zh' : 'en')}
          >
            {language === 'en' ? '中文' : 'EN'}
          </button>
        </div>
        <div className="login-header">
          <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#4a9eff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
            <line x1="16" y1="13" x2="8" y2="13" />
            <line x1="16" y1="17" x2="8" y2="17" />
            <polyline points="10 9 9 9 8 9" />
          </svg>
          <h1>{t('login.title')}</h1>
          <p className="login-subtitle">{t('login.subtitle')}</p>
        </div>

        {error && <div className="login-error">{error}</div>}
        {info && <div className="login-info">{info}</div>}

        {mode === 'signin' && (
          <form onSubmit={handleSignIn}>
            <div className="form-group">
              <label>{t('login.email')}</label>
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </div>
            <div className="form-group">
              <label>{t('login.password')}</label>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            </div>
            <button type="submit" className="login-button" disabled={isLoading}>
              {isLoading ? t('login.signingIn') : t('login.signIn')}
            </button>
            <div className="login-switch">
              <button type="button" className="link-button" onClick={() => { clearMessages(); setMode('signup'); }}>
                {t('login.noAccount')}
              </button>
            </div>
          </form>
        )}

        {mode === 'signup' && (
          <form onSubmit={handleSignUp}>
            <div className="form-group">
              <label>{t('login.email')}</label>
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </div>
            <div className="form-group">
              <label>{t('login.password')}</label>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            </div>
            <button type="submit" className="login-button" disabled={isLoading}>
              {isLoading ? t('login.signingUp') : t('login.signUp')}
            </button>
            <div className="login-switch">
              <button type="button" className="link-button" onClick={() => { clearMessages(); setMode('signin'); }}>
                {t('login.haveAccount')}
              </button>
            </div>
          </form>
        )}

        {mode === 'confirm' && (
          <form onSubmit={handleConfirm}>
            <div className="form-group">
              <label>{t('login.confirmCode')}</label>
              <input type="text" value={code} onChange={(e) => setCode(e.target.value)} required />
            </div>
            <button type="submit" className="login-button" disabled={isLoading}>
              {isLoading ? t('login.confirming') : t('login.confirm')}
            </button>
          </form>
        )}
      </div>
    </div>
  );
};

export default LoginPage;
