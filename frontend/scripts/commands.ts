import { readFileSync } from 'node:fs';
import { isAbsolute, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const frontendRoot = fileURLToPath(new URL('..', import.meta.url));
const manifest = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')) as {
  engines: { node: string; npm: string };
};

export const resourceLimits = Object.freeze({
  maxWorkers: 2,
  fileParallelism: false,
  maxConcurrency: 1,
  browserWorkers: 1,
  browserFullyParallel: false,
  retries: 0,
});

export function assertRuntime(nodeVersion: string, npmAgent?: string): void {
  if (nodeVersion !== manifest.engines.node) {
    throw new Error('Use the pinned project Node runtime; see frontend/README.md.');
  }
  if (npmAgent && !npmAgent.startsWith(`npm/${manifest.engines.npm} `)) {
    throw new Error('Use the pinned project npm runtime; see frontend/README.md.');
  }
}

function requireIntegrationOrigin(environment: NodeJS.ProcessEnv): string {
  const value = environment.EAI_E2E_BASE_URL;
  if (!value) throw new Error('An explicitly configured HTTPS integration origin is required.');
  let url: URL;
  try { url = new URL(value); } catch { throw new Error('Invalid integration origin.'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) {
    throw new Error('Integration origin must be HTTPS without credentials, query or fragment.');
  }
  return url.href;
}

export interface LaunchPlan {
  entry: string;
  args: string[];
  environment: Record<string, string>;
}

function testArguments(args: string[], suite: string, browser: boolean): string[] {
  const accepted: string[] = [];
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (browser && argument === '--list') { accepted.push(argument); continue; }
    const filter = browser ? argument === '-g' : ['-t', '--testNamePattern'].includes(argument);
    if (filter) {
      const value = args[++index];
      if (!value || value.startsWith('-')) throw new Error('A test-name filter value is required.');
      accepted.push(argument, value);
      continue;
    }
    if (argument.startsWith('-')) {
      throw new Error('This option is not allowed by the resource-bounded test runner.');
    }
    const testRoot = resolve(frontendRoot, 'tests', suite);
    const path = relative(testRoot, resolve(frontendRoot, argument));
    const extension = browser ? /\.spec\.tsx?$/ : /\.test\.tsx?$/;
    if (!path || isAbsolute(path) || path.split(/[\\/]/).includes('..') || !extension.test(path)) {
      throw new Error('Select a test file inside the requested suite.');
    }
    accepted.push(relative(frontendRoot, resolve(testRoot, path)).replaceAll('\\', '/'));
  }
  return accepted;
}

export function createLaunchPlan(
  command: string,
  args: string[] = [],
  environment: NodeJS.ProcessEnv = {},
): LaunchPlan {
  const binary = (path: string) => resolve(frontendRoot, 'node_modules', path);
  if (command === 'test:unit' || command === 'test:integration') {
    const suite = command === 'test:unit' ? 'unit' : 'integration';
    const filters = testArguments(args, suite, false);
    if (suite === 'integration') {
      if (environment.EAI_ALLOW_API_TESTS !== '1') throw new Error('API tests require explicit opt-in.');
      requireIntegrationOrigin(environment);
    }
    return {
      entry: binary('vitest/vitest.mjs'),
      args: ['run', '--config', 'vitest.config.ts', ...filters],
      environment: { EAI_TEST_SUITE: suite },
    };
  }
  if (command === 'test:e2e') {
    const filters = testArguments(args, 'e2e', true);
    if (!filters.includes('--list')) {
      if (environment.EAI_ALLOW_API_TESTS !== '1') throw new Error('Browser integration requires explicit opt-in.');
      requireIntegrationOrigin(environment);
    }
    return { entry: binary('@playwright/test/cli.js'), args: ['test', ...filters], environment: {} };
  }
  if (args.length) throw new Error('This command does not accept extra options.');
  switch (command) {
    case 'typecheck': return { entry: binary('typescript/bin/tsc'), args: ['--noEmit'], environment: {} };
    case 'lint': return { entry: binary('eslint/bin/eslint.js'), args: ['.', '--max-warnings', '0'], environment: {} };
    case 'build': return { entry: binary('vite/bin/vite.js'), args: ['build'], environment: {} };
    case 'dev': return { entry: binary('vite/bin/vite.js'), args: [], environment: {} };
    case 'preview': return { entry: binary('vite/bin/vite.js'), args: ['preview'], environment: {} };
    default: throw new Error('Unknown frontend command.');
  }
}
