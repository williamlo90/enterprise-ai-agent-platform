// @vitest-environment node
import { ESLint } from 'eslint';
import { describe, expect, it } from 'vitest';
import { frontendRoot } from '../../scripts/commands.ts';

describe('lint import boundaries with the pinned dependency graph', () => {
  const eslint = new ESLint({ cwd: frontendRoot });

  it('parses a normal TypeScript API module', async () => {
    const [result] = await eslint.lintText('export const value: number = 1;\n', { filePath: 'src/api/probe.ts' });
    expect(result.errorCount).toBe(0);
  });

  it.each([
    ['src/api/probe.ts', '../features/auth/probe'],
    ['src/api/probe.ts', '../components/probe'],
    ['src/api/probe.ts', '../app/probe'],
    ['src/components/probe.ts', '../api/probe'],
    ['src/components/probe.ts', '../features/auth/probe'],
  ])('rejects the forbidden dependency from %s to %s', async (filePath, dependency) => {
    const [result] = await eslint.lintText(`import { value } from '${dependency}';\nexport const result = value;\n`, { filePath });
    expect(result.messages.some((message) => message.ruleId === 'no-restricted-imports')).toBe(true);
  });
});
