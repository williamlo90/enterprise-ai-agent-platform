import { StrictMode } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { App } from '../../src/app/App';
import { preferenceKeys, readPreferences, savePreference } from '../../src/app/preferences';

beforeEach(() => localStorage.clear());

it('opens in Indonesian/light without writing preferences or calling an API', () => {
  const write = vi.spyOn(Storage.prototype, 'setItem');
  render(<StrictMode><App /></StrictMode>);
  expect(screen.getByRole('heading', { name: 'Pengaturan tampilan', level: 1 })).toBeTruthy();
  expect(document.documentElement.lang).toBe('id');
  expect(document.documentElement.dataset.theme).toBe('light');
  expect(write).not.toHaveBeenCalled();
  expect(globalThis.fetch).not.toHaveBeenCalled();
});

it('changes all page labels, remembers only enum choices, and restores them on remount', async () => {
  const user = userEvent.setup();
  const { unmount } = render(<App />);
  await user.selectOptions(screen.getByLabelText('Bahasa antarmuka'), 'en');
  await user.selectOptions(screen.getByLabelText('Theme'), 'dark');
  expect(screen.getByRole('heading', { name: 'Appearance settings' })).toBeTruthy();
  expect(document.documentElement.lang).toBe('en');
  expect(document.documentElement.dataset.theme).toBe('dark');
  expect(Object.keys(localStorage).sort()).toEqual(Object.values(preferenceKeys).sort());
  expect(localStorage.getItem(preferenceKeys.locale)).toBe('en');
  expect(localStorage.getItem(preferenceKeys.theme)).toBe('dark');
  unmount();
  render(<App />);
  expect((screen.getByLabelText('Interface language') as HTMLSelectElement).value).toBe('en');
  expect((screen.getByLabelText('Theme') as HTMLSelectElement).value).toBe('dark');
  expect(globalThis.fetch).not.toHaveBeenCalled();
});

it('ignores invalid stored values, including markup and private-looking payloads', () => {
  localStorage.setItem(preferenceKeys.locale, '<script>secret</script>');
  localStorage.setItem(preferenceKeys.theme, '{"tenant":"private"}');
  render(<App />);
  expect(screen.getByRole('heading', { name: 'Pengaturan tampilan' })).toBeTruthy();
  expect(document.documentElement.dataset.theme).toBe('light');
  expect(document.body.textContent).not.toContain('secret');
});

it('uses safe defaults when storage access or reads throw', () => {
  expect(readPreferences({ getItem() { throw new Error('denied'); }, setItem() {} })).toEqual({ locale: 'id', theme: 'light' });
  vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new Error('denied'); });
  render(<App />);
  expect(screen.getByRole('heading', { name: 'Pengaturan tampilan' })).toBeTruthy();
  expect(screen.getByRole('status').textContent).toContain('tidak dapat mengingat');
});

it('keeps controls usable and gives an honest session-only message when writes fail', async () => {
  const user = userEvent.setup();
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota'); });
  render(<App />);
  await user.selectOptions(screen.getByLabelText('Bahasa antarmuka'), 'en');
  expect(screen.getByRole('heading', { name: 'Appearance settings' })).toBeTruthy();
  expect(screen.getByRole('status').textContent).toContain('cannot remember');
  expect(localStorage.length).toBe(0);
});

it('refuses values outside the enum allowlist before persistence', () => {
  const storage = { getItem: vi.fn(), setItem: vi.fn() };
  expect(savePreference(storage, 'locale', 'fr')).toBe(false);
  expect(savePreference(storage, 'theme', { tenant: 'private' })).toBe(false);
  expect(savePreference(undefined, 'theme', 'dark')).toBe(false);
  expect(storage.setItem).not.toHaveBeenCalled();
});

it('does not claim both choices are remembered after failed locale and successful theme writes', async () => {
  const user = userEvent.setup();
  vi.spyOn(Storage.prototype, 'setItem').mockImplementationOnce(() => { throw new Error('quota'); });
  render(<App />);
  await user.selectOptions(screen.getByLabelText('Bahasa antarmuka'), 'en');
  await user.selectOptions(screen.getByLabelText('Theme'), 'dark');
  expect(localStorage.getItem(preferenceKeys.locale)).toBeNull();
  expect(localStorage.getItem(preferenceKeys.theme)).toBe('dark');
  expect(screen.getByRole('status').textContent).toContain('cannot remember');
});

it('clearing browser storage restores defaults on the next mount', () => {
  localStorage.setItem(preferenceKeys.locale, 'en');
  localStorage.setItem(preferenceKeys.theme, 'dark');
  const { unmount } = render(<App />);
  unmount();
  localStorage.clear();
  render(<App />);
  expect(screen.getByRole('heading', { name: 'Pengaturan tampilan' })).toBeTruthy();
  expect(document.documentElement.dataset.theme).toBe('light');
});
