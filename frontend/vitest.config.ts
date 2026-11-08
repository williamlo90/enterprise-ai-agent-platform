import process from 'node:process';
import { defineConfig, mergeConfig } from 'vitest/config';
import viteConfig from './vite.config.ts';
import { resourceLimits } from './scripts/commands.ts';

const suite = process.env.EAI_TEST_SUITE ?? 'unit';
if (suite !== 'unit' && suite !== 'integration') throw new Error('Unknown test suite.');

export default mergeConfig(viteConfig, defineConfig({
  test: {
    include: [`tests/${suite}/**/*.test.{ts,tsx}`],
    environment: suite === 'unit' ? 'jsdom' : 'node',
    setupFiles: suite === 'unit' ? ['tests/unit/setup.ts'] : [],
    maxWorkers: resourceLimits.maxWorkers,
    fileParallelism: resourceLimits.fileParallelism,
    maxConcurrency: resourceLimits.maxConcurrency,
    retry: resourceLimits.retries,
    allowOnly: false,
    passWithNoTests: false,
    watch: false,
    testTimeout: 10_000,
    hookTimeout: 10_000,
  },
}));
