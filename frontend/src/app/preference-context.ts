import { createContext, useContext } from 'react';
import type { Locale } from '../locales/messages';
import type { Preferences, Theme } from './preferences';

export interface PreferenceContextValue extends Preferences {
  remembered: boolean;
  setLocale: (locale: Locale) => void;
  setTheme: (theme: Theme) => void;
}
export const PreferenceContext = createContext<PreferenceContextValue | null>(null);
export function usePreferences(): PreferenceContextValue {
  const context = useContext(PreferenceContext);
  if (!context) throw new Error('Preference provider is required.');
  return context;
}
