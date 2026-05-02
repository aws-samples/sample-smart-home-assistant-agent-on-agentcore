import React, { useState, useEffect } from 'react';
import TopNavigation from '@cloudscape-design/components/top-navigation';
import AppLayout from '@cloudscape-design/components/app-layout';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import SideNavigation from '@cloudscape-design/components/side-navigation';
import LoginPage from './auth/LoginPage';
import AdminConsole, { ActiveTab } from './components/AdminConsole';
import { getCurrentSession, signOut, getIsAdmin, AuthTokens } from './auth/CognitoAuth';
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
  const [isAdmin, setIsAdmin] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [theme, setLocalTheme] = useState<Theme>(() => detectInitialTheme());
  const [activeTab, setActiveTab] = useState<ActiveTab>('overview');
  const { t, language, setLanguage } = useI18n();

  useEffect(() => {
    // Re-apply on mount in case system preference has changed since module-load.
    setTheme(theme);
  }, [theme]);

  useEffect(() => {
    getCurrentSession()
      .then(async () => {
        setIsAuthenticated(true);
        const admin = await getIsAdmin();
        setIsAdmin(admin);
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
    const admin = await getIsAdmin();
    setIsAdmin(admin);
  };

  const handleLogout = () => {
    signOut();
    setIsAuthenticated(false);
    setIsAdmin(false);
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
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#0972d3" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'
          ),
          alt: 'logo',
        },
      }}
      utilities={[
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
      i18nStrings={{
        overflowMenuTriggerText: 'More',
        overflowMenuTitleText: 'All',
      }}
    />
  );

  if (!isAdmin) {
    return (
      <>
        <div id="top-nav">{topNav}</div>
        <AppLayout
          toolsHide
          navigationHide
          headerSelector="#top-nav"
          content={
            <SpaceBetween size="l">
              <Header variant="h1">{t('app.accessDenied')}</Header>
              <Alert type="error">
                <span dangerouslySetInnerHTML={{ __html: t('app.accessDeniedMsg') }} />
              </Alert>
              <Box>
                <Button onClick={handleLogout}>{t('app.signOut')}</Button>
              </Box>
            </SpaceBetween>
          }
        />
      </>
    );
  }

  const navItems = [
    {
      type: 'section' as const,
      text: t('nav.discover'),
      defaultExpanded: true,
      items: [
        { type: 'link' as const, text: t('tab.overview'), href: '#/overview' },
        { type: 'link' as const, text: t('tab.integrations'), href: '#/integrations' },
      ],
    },
    {
      type: 'section' as const,
      text: t('nav.build'),
      defaultExpanded: true,
      items: [
        { type: 'link' as const, text: t('tab.models'), href: '#/models' },
        { type: 'link' as const, text: t('tab.skills'), href: '#/skills' },
        { type: 'link' as const, text: t('nav.prompt'), href: '#/agentPrompts' },
        { type: 'link' as const, text: t('nav.toolPolicy'), href: '#/users' },
        { type: 'link' as const, text: t('tab.memories'), href: '#/memories' },
        { type: 'link' as const, text: t('tab.knowledgeBase'), href: '#/knowledgeBase' },
        { type: 'link' as const, text: t('tab.identity'), href: '#/identity' },
      ],
    },
    {
      type: 'section' as const,
      text: t('nav.deploy'),
      defaultExpanded: true,
      items: [
        { type: 'link' as const, text: t('tab.instanceType'), href: '#/instanceType' },
        { type: 'link' as const, text: t('tab.sessions'), href: '#/sessions' },
      ],
    },
    {
      type: 'section' as const,
      text: t('nav.assess'),
      defaultExpanded: true,
      items: [
        { type: 'link' as const, text: t('nav.agentGuardrails'), href: '#/guardrails' },
        { type: 'link' as const, text: t('tab.observability'), href: '#/observability' },
        { type: 'link' as const, text: t('tab.evaluations'), href: '#/evaluations' },
      ],
    },
    { type: 'divider' as const },
    {
      type: 'link' as const,
      text: t('nav.docs'),
      href: 'https://github.com/aws-samples/smarthome-assistant-agent',
      external: true,
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
            header={{ href: '#/overview', text: t('app.title') }}
            activeHref={`#/${activeTab}`}
            onFollow={(event) => {
              const href = event.detail.href;
              if (href.startsWith('#/')) {
                event.preventDefault();
                setActiveTab(href.slice(2) as ActiveTab);
              }
            }}
            items={navItems}
          />
        }
        content={<AdminConsole activeTab={activeTab} setActiveTab={setActiveTab} />}
      />
    </>
  );
};

export default App;
