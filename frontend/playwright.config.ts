import process from 'node:process';
import { defineConfig, devices } from '@playwright/test';
import { resourceLimits } from './scripts/commands.ts';

export default defineConfig({
  testDir: './tests/e2e',
  workers: resourceLimits.browserWorkers,
  fullyParallel: resourceLimits.browserFullyParallel,
  retries: resourceLimits.retries,
  forbidOnly: true,
  timeout: 30_000,
  reporter: 'list',
  outputDir: '../local/frontend-e2e',
  use: {
    baseURL: process.env.EAI_E2E_BASE_URL,
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  // No webServer: browser integration must use an explicitly prepared environment.
});
