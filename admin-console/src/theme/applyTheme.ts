import { applyMode, Mode } from '@cloudscape-design/global-styles';

const STORAGE_KEY = 'admin.theme';

export type Theme = 'light' | 'dark';

export function detectInitialTheme(): Theme {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'light' || saved === 'dark') return saved;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

// Stable aliases for the console's own CSS (App.css), sampled from Cloudscape's
// visual-refresh design tokens. Same mechanism as the chatbot's `--chat-*` and the
// simulator's `--sim-*`, and it exists for the same reason: Cloudscape hashes its
// component CSS variables at build time, so `var(--color-text-body-default)` cannot
// be referenced from app CSS — the name never resolves and the fallback wins.
//
// This was added late. App.css was written dark-only, with ~65 rules hardcoding
// colours like `#e0e0e0` and `#8888aa`. Those are correct on a dark surface and
// nearly invisible on a light one: the Tool Policy permission list measured a
// contrast ratio of 1.32:1 against white (WCAG AA wants 4.5:1), which made working
// checkboxes look disabled and read as "per-user tool permissions are broken".
// Nothing was broken — all 17 checkboxes were interactive the whole time.
const ADMIN_TOKENS: Record<Theme, Record<string, string>> = {
  light: {
    '--admin-text': '#0f141a',            // text-body-default
    '--admin-text-secondary': '#656871',  // text-body-secondary
    // Dimmest tier, for counts and secondary metadata. Chosen by MEASURING, not by
    // eye: the first pick (#8c8c94) came out at 3.34:1 on white and the second
    // (#767681) at 4.49:1 — both below the 4.5:1 AA floor for body text, the second
    // only just. #74747f is 4.62:1. There is very little room between "dimmer than
    // secondary" and "fails contrast", which is the reason this tier is only
    // slightly lighter than --admin-text-secondary rather than obviously so.
    '--admin-text-dim': '#74747f',
    '--admin-text-strong': '#000716',     // headings
    '--admin-text-inverse': '#ffffff',    // on a filled primary button
    '--admin-link': '#006ce0',
    '--admin-border': '#c6c6cd',
    '--admin-panel': '#f8f8fa',
    '--admin-surface': '#ffffff',
    '--admin-danger': '#d91515',
    '--admin-success': '#037f0c',
  },
  dark: {
    '--admin-text': '#d1d5db',
    '--admin-text-secondary': '#b6bec9',
    '--admin-text-dim': '#8d99a8',
    '--admin-text-strong': '#f9f9fa',
    '--admin-text-inverse': '#000716',
    '--admin-link': '#89bdee',
    '--admin-border': '#424650',
    '--admin-panel': '#0f141a',
    '--admin-surface': '#161d26',
    '--admin-danger': '#ff5d64',
    '--admin-success': '#00a1b2',
  },
};

function injectAdminTokens(theme: Theme): void {
  const root = document.body;
  for (const [k, v] of Object.entries(ADMIN_TOKENS[theme])) {
    root.style.setProperty(k, v);
  }
}

export function setTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme);
  applyMode(theme === 'dark' ? Mode.Dark : Mode.Light);
  injectAdminTokens(theme);
}
