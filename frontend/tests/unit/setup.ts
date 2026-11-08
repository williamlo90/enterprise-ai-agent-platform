import { afterEach, beforeEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('Browser network is disabled in unit tests.'))));
  vi.stubGlobal('XMLHttpRequest', class {
    constructor() { throw new Error('Browser network is disabled in unit tests.'); }
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
