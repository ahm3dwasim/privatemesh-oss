import { defineConfig } from '@playwright/test';

// The "Try it live" landing, proven against a MOCKED `/v2/demo/*` — `e2e-demo/demo.spec.ts` installs
// the routes with `page.route()`, so this never calls a real model and needs no key. A separate
// config from `playwright.config.ts` because that suite builds with `NEXT_PUBLIC_API_MODE=sample`;
// this one needs `NEXT_PUBLIC_API_MODE=demo`, which is a different build. The port is neither 8765
// (the owner's own server) nor 3102/3131/3132 (the other two suites' web servers).
const PORT = Number(process.env.PM_DEMO_WEB_PORT ?? 3151);
export default defineConfig({
  testDir: './e2e-demo', timeout: 45000, expect: { timeout: 12000 }, fullyParallel: false, workers: 1,
  reporter: [['list']],
  use: { baseURL: `http://127.0.0.1:${PORT}`, trace: 'retain-on-failure', launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 900 } } },
    // The brief's own mobile width for this lane's screenshots, distinct from the other suites'
    // 390×844 phone project.
    { name: 'mobile375', use: { viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true } },
  ],
  webServer: {
    command: `NEXT_PUBLIC_API_MODE=demo npm run build && NEXT_PUBLIC_API_MODE=demo npm run start -- -p ${PORT}`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: false,
    timeout: 180000,
  },
});
