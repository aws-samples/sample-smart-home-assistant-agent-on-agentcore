import { applyMode, Mode } from '@cloudscape-design/global-styles';

const STORAGE_KEY = 'chatbot.theme';

export type Theme = 'light' | 'dark';

export function detectInitialTheme(): Theme {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'light' || saved === 'dark') return saved;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

// Values copied from @cloudscape-design/design-tokens/index-visual-refresh.json
// (visual-refresh theme). Cloudscape emits component-scoped CSS vars with a
// build-time hash suffix, which we can't reference directly from app CSS, so
// we sample the resolved light/dark values here and push them into stable
// `--chat-*` custom properties on the body element.
const CHAT_TOKENS: Record<Theme, Record<string, string>> = {
  light: {
    '--chat-bubble-incoming-bg': '#f6f6f9',
    '--chat-bubble-incoming-text': '#0f141a',
    '--chat-bubble-outgoing-bg': '#0972d3',  // primary button default (light)
    '--chat-bubble-outgoing-text': '#ffffff',
    '--chat-container-bg': '#ffffff',
    '--chat-text-default': '#0f141a',
    '--chat-text-secondary': '#656871',
    '--chat-border': '#c6c6cd',
    '--chat-primary': '#0972d3',
    '--chat-link': '#006ce0',
    '--chat-panel': '#f8f8fa',
    '--chat-radius': '16px',
    '--chat-shadow': '0 1px 1px 1px rgba(233, 235, 237, 1), 0 6px 36px rgba(0, 7, 22, 0.1)',
  },
  dark: {
    '--chat-bubble-incoming-bg': '#0f141a',
    '--chat-bubble-incoming-text': '#d1d5db',
    '--chat-bubble-outgoing-bg': '#539fe5',  // primary button default (dark)
    '--chat-bubble-outgoing-text': '#000716',
    '--chat-container-bg': '#161d26',
    '--chat-text-default': '#d1d5db',
    '--chat-text-secondary': '#b6bec9',
    '--chat-border': '#424650',
    '--chat-primary': '#539fe5',
    '--chat-link': '#89bdee',
    '--chat-panel': '#0f141a',
    '--chat-radius': '16px',
    '--chat-shadow': '0 1px 1px 1px rgba(0, 7, 22, 1), 0 6px 36px rgba(0, 7, 22, 0.4)',
  },
};

function injectChatTokens(theme: Theme): void {
  const root = document.body;
  const palette = CHAT_TOKENS[theme];
  for (const [k, v] of Object.entries(palette)) {
    root.style.setProperty(k, v);
  }
}

export function setTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme);
  applyMode(theme === 'dark' ? Mode.Dark : Mode.Light);
  injectChatTokens(theme);
}
