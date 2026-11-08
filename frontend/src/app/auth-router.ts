import { createElement } from 'react';
import { createBrowserRouter } from 'react-router';
import { AuthScreen } from './AuthScreen';
import type { AuthPages } from './AuthScreen';
import type { AuthController } from './auth-controller';
import type { ApiCoordinator } from './api-coordinator';

// Explicit composition outside React render avoids duplicate router/window listeners in StrictMode.
export function createAuthRouter(auth: AuthController, pages?: AuthPages, api?:ApiCoordinator) {
  return createBrowserRouter([{ path: '*', element: createElement(AuthScreen, { auth, pages, api }) }]);
}
