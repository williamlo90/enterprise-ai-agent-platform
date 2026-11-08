import { useState } from 'react';
import type { ReactNode } from 'react';
import { RouterProvider } from 'react-router';
import type { ComponentProps } from 'react';
import { AppShell } from '../components/AppShell';
import { Button } from '../components/Button';
import { SelectField } from '../components/Fields';
import { Modal } from '../components/Modal';
import { StatusMessage } from '../components/StatusMessage';
import { messages } from '../locales/messages';
import { PreferenceProvider } from './PreferenceProvider';
import { usePreferences } from './preference-context';
import '../styles/app.css';

export function AppearancePage({ returnLink }: { returnLink?: ReactNode }) {
  const { locale, theme, setLocale, setTheme, remembered } = usePreferences();
  const copy = messages[locale];
  const [aboutOpen, setAboutOpen] = useState(false);
  return <AppShell labels={copy} navigation={<a className="nav-link" href="/" aria-current="page">{copy.appearance}</a>}>
    {returnLink}
    <div className="page-heading"><h1>{copy.heading}</h1><p>{copy.introduction}</p></div>
    <section className="settings-section" aria-labelledby="preferences-heading">
      <div className="settings-description"><h2 id="preferences-heading">{copy.preferences}</h2><p>{copy.preferencesHint}</p></div>
      <div className="settings-fields">
        <SelectField label={copy.language} hint={copy.languageHint} value={locale}
          onChange={(event) => { if (event.target.value === 'id' || event.target.value === 'en') setLocale(event.target.value); }}
          options={[{ value: 'id', label: 'Indonesia', lang: 'id' }, { value: 'en', label: 'English', lang: 'en' }]} />
        <SelectField label={copy.theme} hint={copy.themeHint} value={theme}
          onChange={(event) => { if (event.target.value === 'light' || event.target.value === 'dark') setTheme(event.target.value); }}
          options={[{ value: 'light', label: copy.light }, { value: 'dark', label: copy.dark }]} />
        <p className="preference-note" role="status">{remembered ? copy.storageNote : copy.storageUnavailable}</p>
        <Button variant="quiet" onClick={() => setAboutOpen(true)}>{copy.about}</Button>
      </div>
    </section>
    <StatusMessage kind="empty" title={copy.readyTitle}><p>{copy.readyDescription}</p></StatusMessage>
    <Modal open={aboutOpen} title={copy.aboutTitle} description={copy.aboutDescription}
      closeLabel={copy.close} onClose={() => setAboutOpen(false)} />
  </AppShell>;
}

export function App({ router }: { router?: ComponentProps<typeof RouterProvider>['router'] }) {
  return <PreferenceProvider>{router ? <RouterProvider router={router} /> : <AppearancePage />}</PreferenceProvider>;
}
