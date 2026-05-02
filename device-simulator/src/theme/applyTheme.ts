import { applyMode, Mode } from '@cloudscape-design/global-styles';

const STORAGE_KEY = 'devsim.theme';

export type Theme = 'light' | 'dark';

export function detectInitialTheme(): Theme {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'light' || saved === 'dark') return saved;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

// Cloudscape tokens for the device widgets (Fan, LedMatrix, Oven, RiceCooker).
// Same approach as the chatbot — values sampled from Cloudscape's
// visual-refresh design-tokens JSON and piped through stable `--sim-*` aliases
// so the widget CSS can reference them without dealing with hashed component
// vars.
const SIM_TOKENS: Record<Theme, Record<string, string>> = {
  light: {
    '--sim-card-bg': '#ffffff',
    '--sim-card-border': '#e9ebed',
    '--sim-text-default': '#0f141a',
    '--sim-text-secondary': '#656871',
    '--sim-panel-bg': '#f8f8fa',
    '--sim-button-bg': '#ffffff',
    '--sim-button-border': '#7d8998',
    '--sim-button-text': '#0972d3',
    '--sim-button-active-bg': '#f2f8fd',
    '--sim-button-active-border': '#0972d3',
    '--sim-success': '#037f0c',
    '--sim-warning': '#855900',
    '--sim-error': '#d91515',
    '--sim-primary': '#0972d3',
    '--sim-radius': '16px',
    '--sim-shadow': '0 1px 1px 1px rgba(233, 235, 237, 1), 0 6px 36px rgba(0, 7, 22, 0.1)',
  },
  dark: {
    '--sim-card-bg': '#161d26',
    '--sim-card-border': '#424650',
    '--sim-text-default': '#d1d5db',
    '--sim-text-secondary': '#b6bec9',
    '--sim-panel-bg': '#0f141a',
    '--sim-button-bg': '#0f141a',
    '--sim-button-border': '#656871',
    '--sim-button-text': '#539fe5',
    '--sim-button-active-bg': '#192534',
    '--sim-button-active-border': '#539fe5',
    '--sim-success': '#00a1b2',
    '--sim-warning': '#cc8800',
    '--sim-error': '#ff5d64',
    '--sim-primary': '#539fe5',
    '--sim-radius': '16px',
    '--sim-shadow': '0 1px 1px 1px rgba(0, 7, 22, 1), 0 6px 36px rgba(0, 7, 22, 0.4)',
  },
};

function injectSimTokens(theme: Theme): void {
  const root = document.body;
  for (const [k, v] of Object.entries(SIM_TOKENS[theme])) {
    root.style.setProperty(k, v);
  }
}

export function setTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme);
  applyMode(theme === 'dark' ? Mode.Dark : Mode.Light);
  injectSimTokens(theme);
}
