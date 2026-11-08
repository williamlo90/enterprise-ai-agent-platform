import { StrictMode } from 'react';
import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { App } from '../../src/app/App';

it('mounts the bootstrap without API calls or browser persistence', () => {
  const storage = vi.spyOn(Storage.prototype, 'setItem');
  render(<StrictMode><App /></StrictMode>);
  expect(screen.getByRole('main')).toBeTruthy();
  expect(screen.getByRole('heading', { level: 1 })).toBeTruthy();
  expect(globalThis.fetch).not.toHaveBeenCalled();
  expect(storage).not.toHaveBeenCalled();
});
