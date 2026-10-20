import { defineConfig } from '@playwright/test';

import { API, API_PORT, GATE, GATE_DATA_DIR, GATE_PORT, GATE_TOKEN_FILE, WEB, WEB_PORT } from './e2e-live/live-env';

// The judges' path, run for real: a browser on a `NEXT_PUBLIC_API_MODE=live` build talking to an
// actual `privatemesh serve --v2 --sample` process over HTTP. `playwright.config.ts` is the sample
// suite and is untouched — there the client answers itself from `web/lib/sample.ts` and no server
// is contacted at all, so the two suites prove different claims and neither can stand in for the
// other.
//
// Three servers, all started here:
//
// * the sample backend, on a port that is NOT 8765. The owner's own server may be running there,
//   and a test that binds it, or that kills a process it did not start, is not a test — it is an
//   outage. `PM_WEB_ORIGINS` is the web origin because the browser is a different origin from the
//   API and CORS is the grant that lets it read the response.
// * the web app, built with the live client and pointed at that backend.
// * a LIVE backend, which the sample cannot stand in for. Sample mode is open by design, so the
//   only server that can answer "does a private route refuse a caller with no token" is a live
//   one — `e2e-live/gate.spec.ts`, and `e2e-live/live-backend.py` for why it can start without
//   credentials. Its token is minted to a tmp path the spec reads, never into this tree.
export default defineConfig({
  testDir: './e2e-live',
  // The journey drives a real learning loop, which runs generated code in the sandbox and a review
  // pass over it, and polls a 600 ms UI ticker between stages. The sample suite's 45 s is not
  // enough for it.
  timeout: 120000,
  expect: { timeout: 20000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: WEB,
    trace: 'retain-on-failure',
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH },
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'phone', use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
  webServer: [
    {
      command: `.venv/bin/python -m privatemesh.cli serve --v2 --sample 127.0.0.1 ${API_PORT}`,
      cwd: '..',
      url: `${API}/health`,
      env: { ...process.env, PM_WEB_ORIGINS: WEB } as Record<string, string>,
      reuseExistingServer: false,
      timeout: 120000,
    },
    {
      command: `npm run build && npm run start -- -p ${WEB_PORT}`,
      url: WEB,
      env: {
        ...process.env,
        NEXT_PUBLIC_API_MODE: 'live',
        NEXT_PUBLIC_API_BASE_URL: API,
      } as Record<string, string>,
      reuseExistingServer: false,
      timeout: 300000,
    },
    {
      command: `.venv/bin/python web/e2e-live/live-backend.py ${GATE_PORT}`,
      cwd: '..',
      url: `${GATE}/health`,
      env: {
        ...process.env,
        PM_WEB_ORIGINS: WEB,
        PM_OWNER_TOKEN_FILE: GATE_TOKEN_FILE,
        PM_E2E_DATA_DIR: GATE_DATA_DIR,
      } as Record<string, string>,
      reuseExistingServer: false,
      timeout: 120000,
    },
  ],
});
