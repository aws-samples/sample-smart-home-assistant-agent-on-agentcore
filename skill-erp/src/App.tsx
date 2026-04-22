import React, { useState, useEffect } from 'react';
import LoginPage from './auth/LoginPage';
import SkillManager from './components/SkillManager';
import { getCurrentSession, signOut, getCurrentUserEmail, AuthTokens } from './auth/CognitoAuth';
import { useI18n } from './i18n';
import './App.css';

const App: React.FC = () => {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [email, setEmail] = useState('');
  const { t, language, setLanguage } = useI18n();

  useEffect(() => {
    getCurrentSession()
      .then(async () => {
        setIsAuthenticated(true);
        setEmail(await getCurrentUserEmail());
      })
      .catch(() => {
        setIsAuthenticated(false);
      })
      .finally(() => {
        setIsLoading(false);
      });
  }, []);

  const handleAuthenticated = async (_tokens: AuthTokens) => {
    setIsAuthenticated(true);
    setEmail(await getCurrentUserEmail());
  };

  const handleLogout = () => {
    signOut();
    setIsAuthenticated(false);
    setEmail('');
  };

  if (isLoading) {
    return (
      <div className="loading-screen">
        <div className="loading-spinner" />
        <p className="loading-text">{t('app.loading')}</p>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <LoginPage onAuthenticated={handleAuthenticated} />;
  }

  return (
    <div className="app-layout">
      <header className="app-header">
        <div className="header-left">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#4a9eff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
            <line x1="16" y1="13" x2="8" y2="13" />
            <line x1="16" y1="17" x2="8" y2="17" />
            <polyline points="10 9 9 9 8 9" />
          </svg>
          <div>
            <h1 className="header-title">{t('app.title')}</h1>
            <p className="header-subtitle">{t('app.header.subtitle')}</p>
          </div>
        </div>
        <div className="header-right">
          {email && <span className="header-user">{email}</span>}
          <button
            className="lang-switch"
            onClick={() => setLanguage(language === 'en' ? 'zh' : 'en')}
          >
            {language === 'en' ? '中文' : 'EN'}
          </button>
          <button className="logout-button" onClick={handleLogout}>
            {t('app.signOut')}
          </button>
        </div>
      </header>
      <main className="app-main">
        <SkillManager />
      </main>
    </div>
  );
};

export default App;
