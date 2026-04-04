import React, { useEffect, useState } from 'react';
import { MqttClient } from './mqtt/MqttClient';
import LedMatrix from './components/LedMatrix';
import RiceCooker from './components/RiceCooker';
import Fan from './components/Fan';
import Oven from './components/Oven';

const App: React.FC = () => {
  const [connected, setConnected] = useState(false);

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
        <h1>Smart Home Device Simulator</h1>
        <div className="connection-status">
          <span className={`status-dot ${connected ? 'connected' : 'disconnected'}`} />
          <span className="status-text">
            {connected ? 'Connected to AWS IoT' : 'Disconnected'}
          </span>
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
