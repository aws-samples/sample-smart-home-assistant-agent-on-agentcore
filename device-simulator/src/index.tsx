import React from 'react';
import { createRoot } from 'react-dom/client';
import '@cloudscape-design/global-styles/index.css';
import { detectInitialTheme, setTheme } from './theme/applyTheme';
import App from './App';
import { I18nProvider } from './i18n';
import './App.css';

setTheme(detectInitialTheme());

const container = document.getElementById('root');
if (container) {
  const root = createRoot(container);
  root.render(
    <React.StrictMode>
      <I18nProvider>
        <App />
      </I18nProvider>
    </React.StrictMode>
  );
}
