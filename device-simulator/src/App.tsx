import React, { useEffect, useState } from 'react';
import { MqttClient } from './mqtt/MqttClient';
import LedMatrix from './components/LedMatrix';
import RiceCooker from './components/RiceCooker';
import Fan from './components/Fan';
import Oven from './components/Oven';
import { useI18n } from './i18n';

const App: React.FC = () => {
  const [connected, setConnected] = useState(false);
  const { t, language, setLanguage } = useI18n();

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

  return (
    <div className="app">
      <header className="app-header">
        <h1>{t('app.title')}</h1>
        <div className="header-right">
          <div className="connection-status">
            <span className={`status-dot ${connected ? 'connected' : 'disconnected'}`} />
            <span className="status-text">
              {connected ? t('app.connected') : t('app.disconnected')}
            </span>
          </div>
          <button
            className="lang-switch"
            onClick={() => setLanguage(language === 'en' ? 'zh' : 'en')}
          >
            {language === 'en' ? '中文' : 'EN'}
          </button>
        </div>
      </header>
      <div className="dashboard-grid">
        <LedMatrix />
        <RiceCooker />
        <Fan />
        <Oven />
      </div>
    </div>
  );
};

export default App;
