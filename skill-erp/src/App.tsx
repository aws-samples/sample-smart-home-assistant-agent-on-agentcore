import React, { useState, useEffect } from 'react';
import TopNavigation from '@cloudscape-design/components/top-navigation';
import AppLayout from '@cloudscape-design/components/app-layout';
import SideNavigation from '@cloudscape-design/components/side-navigation';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import LoginPage from './auth/LoginPage';
import SkillManager from './components/SkillManager';
import A2AAgentsTab from './components/A2AAgentsTab';
import { ErpTab } from './components/TabBar';
import { getCurrentSession, signOut, getCurrentUserEmail, AuthTokens } from './auth/CognitoAuth';
import { useI18n } from './i18n';
import { detectInitialTheme, setTheme, Theme } from './theme/applyTheme';
import './App.css';

const SUN_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="5" />
    <line x1="12" y1="1" x2="12" y2="3" />
    <line x1="12" y1="21" x2="12" y2="23" />
    <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
    <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
    <line x1="1" y1="12" x2="3" y2="12" />
    <line x1="21" y1="12" x2="23" y2="12" />
    <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
    <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
  </svg>
);
const MOON_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
  </svg>
);

const App: React.FC = () => {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [email, setEmail] = useState('');
  const [activeTab, setActiveTab] = useState<ErpTab>('skills');
  const [theme, setLocalTheme] = useState<Theme>(() => detectInitialTheme());
  const { t, language, setLanguage } = useI18n();

  useEffect(() => {
    setTheme(theme);
  }, [theme]);

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
      <Box textAlign="center" padding="xxxl">
        <SpaceBetween size="m" direction="vertical">
          <Spinner size="large" />
          <Box color="text-body-secondary">{t('app.loading')}</Box>
        </SpaceBetween>
      </Box>
    );
  }

  if (!isAuthenticated) {
    return <LoginPage onAuthenticated={handleAuthenticated} />;
  }

  const topNav = (
    <TopNavigation
      identity={{
        href: '#',
        title: t('app.title'),
        logo: {
          src: 'data:image/svg+xml;utf8,' + encodeURIComponent(
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#0972d3" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>'
          ),
          alt: 'logo',
        },
      }}
      utilities={[
        ...(email ? [{
          type: 'button' as const,
          text: email,
          disableUtilityCollapse: false,
        }] : []),
        {
          type: 'button',
          text: language === 'en' ? '中文' : 'EN',
          ariaLabel: 'Language',
          onClick: () => setLanguage(language === 'en' ? 'zh' : 'en'),
        },
        {
          type: 'button',
          iconSvg: theme === 'dark' ? SUN_ICON : MOON_ICON,
          ariaLabel: theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode',
          onClick: () => {
            const next: Theme = theme === 'dark' ? 'light' : 'dark';
            setTheme(next);
            setLocalTheme(next);
          },
        },
        {
          type: 'button',
          text: t('app.signOut'),
          iconName: 'external',
          onClick: handleLogout,
        },
      ]}
      i18nStrings={{ overflowMenuTriggerText: 'More', overflowMenuTitleText: 'All' }}
    />
  );

  const navItems = [
    {
      type: 'section' as const,
      text: t('erp.nav.publish'),
      defaultExpanded: true,
      items: [
        { type: 'link' as const, text: t('erp.tab.skills'), href: '#/skills' },
        { type: 'link' as const, text: t('erp.tab.a2a'), href: '#/a2a' },
      ],
    },
  ];

  return (
    <>
      <div id="top-nav">{topNav}</div>
      <AppLayout
        toolsHide
        headerSelector="#top-nav"
        navigation={
          <SideNavigation
            header={{ href: '#/skills', text: t('app.title') }}
            activeHref={`#/${activeTab}`}
            onFollow={(event) => {
              const href = event.detail.href;
              if (href.startsWith('#/')) {
                event.preventDefault();
                setActiveTab(href.slice(2) as ErpTab);
              }
            }}
            items={navItems}
          />
        }
        content={activeTab === 'skills' ? <SkillManager /> : <A2AAgentsTab />}
      />
    </>
  );
};

export default App;
