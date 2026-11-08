import { readFileSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import type { Plugin } from 'vite';

// Ship notices with static assets, including production dependency license/notice texts.
export function notices(root: string): Plugin {
  return { name: 'third-party-notices', generateBundle() {
    const lock = JSON.parse(readFileSync(resolve(root, 'package-lock.json'), 'utf8')) as { packages: Record<string, { dev?: boolean }> };
    const sections = [readFileSync(resolve(root, 'THIRD_PARTY_NOTICES.md'), 'utf8')];
    for (const [path, meta] of Object.entries(lock.packages)) {
      if (!path || meta.dev) continue;
      const directory = resolve(root, path);
      for (const file of readdirSync(directory)) {
        if (/^(?:licen[cs]e|notice)(?:\.[a-z]+)?$/i.test(file)) sections.push(`\n## ${path}/${file}\n\n${readFileSync(resolve(directory, file), 'utf8')}`);
      }
    }
    this.emitFile({ type: 'asset', fileName: 'THIRD_PARTY_NOTICES.txt', source: sections.join('\n') });
  } };
}
