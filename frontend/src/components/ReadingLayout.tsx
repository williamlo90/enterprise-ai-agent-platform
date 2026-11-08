import type { ReactNode } from 'react';

export function ReadingLayout({ children, sources, sourceLabel }: { children: ReactNode; sources: ReactNode; sourceLabel: string }) {
  return <div className="reading-layout"><section className="reading-main">{children}</section>
    <aside className="reading-sources" aria-label={sourceLabel}>{sources}</aside></div>;
}
