import { useState } from 'react';
import type { ReactNode } from 'react';
import { Button } from './Button';
import { Modal } from './Modal';

interface AppShellProps {
  labels: { skip: string; navigation: string; menu: string; closeMenu: string; publicScope: string; publicView: string };
  navigation: ReactNode; children: ReactNode; actions?: ReactNode; renderHomeLink?: (children: ReactNode) => ReactNode;
}
export function AppShell({ labels, navigation, children, actions, renderHomeLink }: AppShellProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const closeMenu = () => setMenuOpen(false);
  const brandContent = <><svg className="brand-mark" viewBox="0 0 28 28" aria-hidden="true"><path d="M5 5h13v18H5zM10 9h13v14H10M8 10h6M8 14h6M13 18h7" /></svg>
    <span>Enterprise AI<span className="brand-subtitle">Agent Platform</span></span></>;
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">{labels.skip}</a>
    <header className="app-header">
      {renderHomeLink ? renderHomeLink(brandContent) : <a className="brand" href="/" aria-label="Enterprise AI Agent Platform">{brandContent}</a>}
      <span className="public-scope">{labels.publicView}</span>
      {actions && <div className="session-controls">{actions}</div>}
      <Button className="mobile-menu-button" aria-haspopup="dialog" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}>{labels.menu}</Button>
    </header>
    <aside className="sidebar">
      <p className="scope-label">{labels.publicScope}</p>
      <nav aria-label={labels.navigation}>{navigation}</nav>
      <p className="sidebar-footnote">Enterprise AI Agent Platform</p>
    </aside>
    <main id="main-content" tabIndex={-1} className="main-content">{children}</main>
    <Modal open={menuOpen} title={labels.navigation} closeLabel={labels.closeMenu} onClose={closeMenu}>
      <nav aria-label={labels.navigation} onClick={(event) => { if ((event.target as HTMLElement).closest('a')) closeMenu(); }}>{navigation}</nav>
    </Modal>
  </div>;
}
