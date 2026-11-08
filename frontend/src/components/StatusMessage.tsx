import type { ReactNode } from 'react';

export function StatusMessage({ kind = 'info', title, children }: {
  kind?: 'info' | 'loading' | 'empty' | 'success' | 'error'; title: string; children?: ReactNode;
}) {
  return <section className={`status-message status-message--${kind}`}
    role={kind === 'error' ? 'alert' : kind === 'loading' || kind === 'success' ? 'status' : undefined}
    aria-busy={kind === 'loading' || undefined}>
    <h2>{title}</h2>
    {children && <div className="status-description">{children}</div>}
  </section>;
}
