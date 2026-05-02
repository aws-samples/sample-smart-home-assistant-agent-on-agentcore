import React from 'react';
import { createRoot } from 'react-dom/client';
import '@cloudscape-design/global-styles/index.css';
import { detectInitialTheme, setTheme } from './theme/applyTheme';
import App from './App';
import { I18nProvider } from './i18n';

// Apply the persisted/preferred theme before React mounts so the first
// paint is correct (no light-flash on reload for dark-mode users).
setTheme(detectInitialTheme());

const container = document.getElementById('root');
if (container) {
  const root = createRoot(container);
  root.render(
    <I18nProvider>
      <App />
    </I18nProvider>
  );
}
