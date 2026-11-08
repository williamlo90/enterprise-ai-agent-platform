// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { assertRuntime, createLaunchPlan } from '../../scripts/commands.ts';

describe('pinned runtime', () => {
  it('accepts the project runtime and rejects an older Node or mismatched npm', () => {
    expect(() => assertRuntime('24.22.2', 'npm/11.19.1 node/v24.22.2 win32 x64')).not.toThrow();
    expect(() => assertRuntime('22.19.0')).toThrow();
    expect(() => assertRuntime('24.22.2', 'npm/10.0.0 node/v24.22.2')).toThrow();
  });
});

describe('resource-bounded commands', () => {
  it('keeps unit files and name filters in the selected suite with run mode', () => {
    const plan = createLaunchPlan('test:unit', ['tests/unit/bootstrap.test.tsx', '-t', 'bootstrap']);
    expect(plan.args).toEqual(['run', '--config', 'vitest.config.ts', 'tests/unit/bootstrap.test.tsx', '-t', 'bootstrap']);
    expect(plan.environment.EAI_TEST_SUITE).toBe('unit');
  });

  it.each(['--maxWorkers=8', '--fileParallelism=true', '--maxConcurrency=8', '--watch', '--config=other.ts', '--shard=1/2', '--browser', '--retry=2'])(
    'rejects an option that can bypass validation policy: %s', (option) => {
      expect(() => createLaunchPlan('test:unit', [option])).toThrow();
    },
  );

  it('rejects paths outside the suite and incomplete filters', () => {
    expect(() => createLaunchPlan('test:unit', ['../backend/tests/private.test.ts'])).toThrow();
    expect(() => createLaunchPlan('test:unit', ['tests/integration/api.test.ts'])).toThrow();
    expect(() => createLaunchPlan('test:unit', ['-t'])).toThrow();
  });

  it('requires opt-in and an HTTPS origin for API integration', () => {
    expect(() => createLaunchPlan('test:integration')).toThrow();
    expect(() => createLaunchPlan('test:integration', [], { EAI_ALLOW_API_TESTS: '1', EAI_E2E_BASE_URL: 'http://example.invalid' })).toThrow();
    const plan = createLaunchPlan('test:integration', [], { EAI_ALLOW_API_TESTS: '1', EAI_E2E_BASE_URL: 'https://example.invalid' });
    expect(plan.environment.EAI_TEST_SUITE).toBe('integration');
  });

  it('can list browser cases offline but cannot launch integration without opt-in', () => {
    expect(createLaunchPlan('test:e2e', ['--list']).args).toEqual(['test', '--list']);
    expect(() => createLaunchPlan('test:e2e')).toThrow();
    expect(() => createLaunchPlan('test:e2e', ['--workers=4'])).toThrow();
  });

  it('rejects credentials or query material in the integration URL', () => {
    for (const origin of ['https://user:password@example.invalid', 'https://example.invalid/?token=sample', 'https://example.invalid/#sample']) {
      expect(() => createLaunchPlan('test:e2e', [], { EAI_ALLOW_API_TESTS: '1', EAI_E2E_BASE_URL: origin })).toThrow();
    }
  });

  it('rejects unknown commands and extra non-test options', () => {
    expect(() => createLaunchPlan('unknown')).toThrow();
    expect(() => createLaunchPlan('dev', ['--host=0.0.0.0'])).toThrow();
  });
});
