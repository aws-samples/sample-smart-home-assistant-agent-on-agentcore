import { applyMode, Mode } from '@cloudscape-design/global-styles';

const STORAGE_KEY = 'devsim.theme';

export type Theme = 'light' | 'dark';

export function detectInitialTheme(): Theme {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'light' || saved === 'dark') return saved;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function setTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme);
  applyMode(theme === 'dark' ? Mode.Dark : Mode.Light);
}
