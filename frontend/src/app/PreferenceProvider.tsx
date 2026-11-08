import { useLayoutEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { PreferenceContext } from './preference-context';
import { browserStorage, loadPreferences, savePreference } from './preferences';
import type { Preferences } from './preferences';

export function PreferenceProvider({ children }: { children: ReactNode }) {
  const [{ preferences, persistence }, setState] = useState(() => loadPreferences(browserStorage()));
  useLayoutEffect(() => {
    const root = document.documentElement;
    const previousLanguage = root.lang;
    const previousTheme = root.getAttribute('data-theme');
    root.lang = preferences.locale;
    root.dataset.theme = preferences.theme;
    return () => {
      root.lang = previousLanguage;
      if (previousTheme === null) root.removeAttribute('data-theme');
      else root.dataset.theme = previousTheme;
    };
  }, [preferences.locale, preferences.theme]);

  function update<K extends keyof Preferences>(key: K, value: Preferences[K]) {
    const persisted = savePreference(browserStorage(), key, value);
    setState((previous) => ({ preferences: { ...previous.preferences, [key]: value },
      persistence: { ...previous.persistence, [key]: persisted } }));
  }
  return <PreferenceContext value={{ ...preferences, remembered: persistence.locale && persistence.theme,
    setLocale: (locale) => update('locale', locale), setTheme: (theme) => update('theme', theme) }}>
    {children}
  </PreferenceContext>;
}
