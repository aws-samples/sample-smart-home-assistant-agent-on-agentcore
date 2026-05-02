import React, { useEffect, useState } from 'react';
import TopNavigation from '@cloudscape-design/components/top-navigation';
import AppLayout from '@cloudscape-design/components/app-layout';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { MqttClient } from './mqtt/MqttClient';
import LedMatrix from './components/LedMatrix';
import RiceCooker from './components/RiceCooker';
import Fan from './components/Fan';
import Oven from './components/Oven';
import { useI18n } from './i18n';
import { detectInitialTheme, setTheme, Theme } from './theme/applyTheme';

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
  const [connected, setConnected] = useState(false);
  const [theme, setLocalTheme] = useState<Theme>(() => detectInitialTheme());
  const { t, language, setLanguage } = useI18n();

  useEffect(() => {
    setTheme(theme);
  }, [theme]);

  useEffect(() => {
    const mqtt = MqttClient.getInstance();
    const unsub = mqtt.onConnectionChange(setConnected);

    mqtt.connect().catch((err) => {
      console.error('Failed to connect MQTT:', err);
    });

    return () => {
      unsub();
    };
  }, []);

  const topNav = (
    <TopNavigation
      identity={{
        href: '#',
        title: t('app.title'),
        logo: {
          src: 'data:image/svg+xml;utf8,' + encodeURIComponent(
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#0972d3" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="12" rx="2"/><line x1="8" y1="20" x2="16" y2="20"/><line x1="12" y1="16" x2="12" y2="20"/></svg>'
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
      ]}
      i18nStrings={{ overflowMenuTriggerText: 'More', overflowMenuTitleText: 'All' }}
    />
  );

  return (
    <>
      <div id="top-nav">{topNav}</div>
      <AppLayout
        toolsHide
        navigationHide
        headerSelector="#top-nav"
        content={
          <div>
            <div className="sim-connection-row">
              {connected ? (
                <StatusIndicator type="success">{t('app.connected')}</StatusIndicator>
              ) : (
                <StatusIndicator type="stopped">{t('app.disconnected')}</StatusIndicator>
              )}
            </div>
            <div className="dashboard-grid">
              <LedMatrix />
              <RiceCooker />
              <Fan />
              <Oven />
            </div>
          </div>
        }
      />
    </>
  );
};

export default App;
