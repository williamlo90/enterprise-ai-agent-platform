export type Locale = 'id' | 'en';

const id = {
  skip: 'Lewati ke isi utama', navigation: 'Navigasi utama', menu: 'Buka navigasi', closeMenu: 'Tutup navigasi',
  appearance: 'Tampilan', publicScope: 'Pengaturan browser', publicView: 'Halaman publik',
  heading: 'Pengaturan tampilan', introduction: 'Sesuaikan bahasa dan tema untuk kenyamanan membaca.',
  preferences: 'Bahasa dan tema', preferencesHint: 'Pilihan berlaku pada browser ini. Anda dapat mengubahnya kapan saja.',
  language: 'Bahasa antarmuka', theme: 'Tema', light: 'Terang', dark: 'Gelap',
  languageHint: 'Pilihan ini tidak menerjemahkan dokumen atau percakapan yang sudah ada.',
  themeHint: 'Pilih tampilan yang nyaman untuk lingkungan kerja Anda.',
  storageNote: 'Hanya pilihan bahasa dan tema yang diingat pada browser ini.',
  storageUnavailable: 'Pilihan berlaku selama halaman ini terbuka. Browser tidak dapat mengingat pengaturan Anda.',
  readyTitle: 'Ruang kerja sedang disiapkan', readyDescription: 'Akses dokumen dan percakapan perusahaan belum tersedia pada halaman ini.',
  about: 'Tentang pengaturan', aboutTitle: 'Pengaturan pada browser ini', close: 'Tutup',
  aboutDescription: 'Bahasa dan tema disimpan pada browser yang sama. Browser baru atau penghapusan data browser mengembalikan pengaturan awal ke Indonesia dan terang.',
  footer: 'Enterprise AI Agent Platform', loading: 'Memuat', error: 'Terjadi kesalahan',
} as const;

type MessageKey = keyof typeof id;
const en: Record<MessageKey, string> = {
  skip: 'Skip to main content', navigation: 'Main navigation', menu: 'Open navigation', closeMenu: 'Close navigation',
  appearance: 'Appearance', publicScope: 'Browser settings', publicView: 'Public page',
  heading: 'Appearance settings', introduction: 'Choose a language and theme for comfortable reading.',
  preferences: 'Language and theme', preferencesHint: 'These choices apply to this browser. You can change them at any time.',
  language: 'Interface language', theme: 'Theme', light: 'Light', dark: 'Dark',
  languageHint: 'This choice does not translate documents or existing conversations.',
  themeHint: 'Choose a comfortable appearance for your working environment.',
  storageNote: 'Only your language and theme choices are remembered in this browser.',
  storageUnavailable: 'Your choices apply while this page is open. This browser cannot remember your settings.',
  readyTitle: 'Your workspace is being prepared', readyDescription: 'Company documents and conversations are not available on this page yet.',
  about: 'About these settings', aboutTitle: 'Settings in this browser', close: 'Close',
  aboutDescription: 'Language and theme are saved in the same browser. A new browser or clearing browser data restores Indonesian and the light theme.',
  footer: 'Enterprise AI Agent Platform', loading: 'Loading', error: 'Something went wrong',
};

export const messages: Record<Locale, Record<MessageKey, string>> = { id, en };
