import type { Locale } from '../locales/messages';

export type Theme = 'light' | 'dark';
export interface Preferences { locale: Locale; theme: Theme }
export const preferenceKeys = { locale: 'eai.ui.locale', theme: 'eai.ui.theme' } as const;
export const defaultPreferences: Preferences = { locale: 'id', theme: 'light' };
export type PreferenceStorage = Pick<Storage, 'getItem' | 'setItem'>;

export function browserStorage(): PreferenceStorage | undefined {
  try { return window.localStorage; } catch { return undefined; }
}

export function loadPreferences(storage?: PreferenceStorage) {
  const fallback = () => ({ preferences: { ...defaultPreferences }, persistence: { locale: false, theme: false } });
  if (!storage) return fallback();
  try {
    const locale = storage.getItem(preferenceKeys.locale);
    const theme = storage.getItem(preferenceKeys.theme);
    const preferences: Preferences = { locale: locale === 'en' ? 'en' : 'id', theme: theme === 'dark' ? 'dark' : 'light' };
    return { preferences, persistence: { locale: true, theme: true } };
  } catch { return fallback(); }
}

export function readPreferences(storage?: PreferenceStorage): Preferences { return loadPreferences(storage).preferences; }

// The public boundary accepts unknown values and writes only the two allowlisted enums.
export function savePreference(storage: PreferenceStorage | undefined, key: keyof Preferences, value: unknown): boolean {
  if ((key === 'locale' && value !== 'id' && value !== 'en') ||
      (key === 'theme' && value !== 'light' && value !== 'dark')) return false;
  if (!storage || !Object.hasOwn(preferenceKeys, key)) return false;
  try { storage.setItem(preferenceKeys[key], String(value)); return true; } catch { return false; }
}
