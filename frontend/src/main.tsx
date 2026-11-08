import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './app/App';
import { startAuthRuntime } from './app/auth-runtime';
import { createAuthRouter } from './app/auth-router';

const container = document.getElementById('root');
if (!container) throw new Error('Application root is unavailable.');
const runtime = __EAI_AUTH_ENABLED__ ? startAuthRuntime() : undefined;
const router = runtime ? createAuthRouter(runtime.auth,undefined,runtime.api) : undefined;
if (import.meta.hot) import.meta.hot.dispose(() => { router?.dispose(); runtime?.dispose(); });

createRoot(container).render(
  <StrictMode>
    <App router={router} />
  </StrictMode>,
);
