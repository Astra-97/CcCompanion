import { createHttpAdapter, createMockAdapter } from './api.js?v=7';

export function isExplicitMockMode(locationLike = globalThis.location, windowLike = globalThis) {
  const query = new URLSearchParams(locationLike?.search || '');
  return query.get('mock') === '1' || windowLike.CC_COMPANION_DEV_MOCK === true;
}

/** Production bootstrap for /web/pwa/: cookie session first, contacts second. */
export function createPwaBootstrap({ locationLike = globalThis.location, windowLike = globalThis, request } = {}) {
  const mock = isExplicitMockMode(locationLike, windowLike);
  const adapter = mock ? createMockAdapter() : createHttpAdapter({ request });
  return {
    mock,
    adapter,
    checkSession: () => adapter.getWebSession(),
    establishSession: ({ username, password }) => adapter.createWebSession({ username, password }),
    establishPairingSession: ({ code }) => adapter.pairWebSession({ code }),
  };
}
