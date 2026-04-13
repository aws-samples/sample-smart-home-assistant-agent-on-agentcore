import React, { useState, useEffect } from 'react';
import LoginPage from './auth/LoginPage';
import ChatInterface from './components/ChatInterface';
import { getCurrentSession, signOut, AuthTokens } from './auth/CognitoAuth';
import { useI18n } from './i18n';

const App: React.FC = () => {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const { t, language, setLanguage } = useI18n();

  useEffect(() => {
    getCurrentSession()
      .then(() => {
        setIsAuthenticated(true);
      })
      .catch(() => {
        setIsAuthenticated(false);
      })
      .finally(() => {
        setIsLoading(false);
      });
  }, []);

  const handleAuthenticated = (_tokens: AuthTokens) => {
    setIsAuthenticated(true);
  };

  const handleLogout = () => {
    signOut();
    setIsAuthenticated(false);
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
            <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
            <polyline points="9 22 9 12 15 12 15 22" />
          </svg>
          <h1 className="header-title">{t('app.title')}</h1>
        </div>
        <div className="header-right">
          <button
            className="lang-switch"
            onClick={() => setLanguage(language === 'en' ? 'zh' : 'en')}
          >
            {language === 'en' ? '中文' : 'EN'}
          </button>
          <button className="logout-button" onClick={handleLogout}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
              <polyline points="16 17 21 12 16 7" />
              <line x1="21" y1="12" x2="9" y2="12" />
            </svg>
            {t('app.signOut')}
          </button>
        </div>
      </header>
      <main className="app-main">
        <ChatInterface />
      </main>
    </div>
  );
};

export default App;
