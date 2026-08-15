import React, { createContext, useContext, useState, useCallback, useMemo } from 'react';
import en from './locales/en';
import zh from './locales/zh';

type Language = 'en' | 'zh';

const STORAGE_KEY = 'smarthome-language';

const locales: Record<Language, Record<string, string>> = { en, zh };

function getInitialLanguage(): Language {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'en' || stored === 'zh') return stored;
  } catch { /* ignore */ }
  return 'en';
}

interface I18nContextValue {
  language: Language;
  setLanguage: (lang: Language) => void;
  t: (key: string, params?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nContextValue>({
  language: 'en',
  setLanguage: () => {},
  t: (key) => key,
});

export const I18nProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [language, setLanguageState] = useState<Language>(getInitialLanguage);

  const setLanguage = useCallback((lang: Language) => {
    setLanguageState(lang);
    try { localStorage.setItem(STORAGE_KEY, lang); } catch { /* ignore */ }
  }, []);

  // `{{name}}` placeholders are substituted rather than having callers concatenate
  // around a translated fragment. Word order differs between the locales — "expire
  // in 5 min" against "5 分钟后撤销" — so a string built by concatenation can only
  // be right in one of them.
  const t = useCallback(
    (key: string, params?: Record<string, string | number>): string => {
      const template = locales[language][key] ?? locales.en[key] ?? key;
      if (!params) return template;
      return Object.entries(params).reduce(
        (out, [name, value]) => out.split(`{{${name}}}`).join(String(value)),
        template,
      );
    },
    [language],
  );

  const value = useMemo(() => ({ language, setLanguage, t }), [language, setLanguage, t]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
};

export const useI18n = () => useContext(I18nContext);
