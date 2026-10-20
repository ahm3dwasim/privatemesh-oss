import { defineConfig } from '@playwright/test';

// Overridable for the same reason the live suite's ports are: two lanes running this suite at once
// on one machine would otherwise fight over one port, and a test that binds a port somebody else is
// serving is not a test. The default is unchanged, so nothing that ran before runs differently.
const PORT = Number(process.env.PM_SAMPLE_WEB_PORT ?? 3102);
export default defineConfig({
  testDir: './e2e', timeout: 45000, expect: { timeout: 12000 }, fullyParallel: false, workers: 1,
  reporter: [['list']],
  use: { baseURL: `http://127.0.0.1:${PORT}`, trace: 'retain-on-failure', launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'phone', use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
  webServer: { command: `NEXT_PUBLIC_API_MODE=sample npm run build && npm run start -- -p ${PORT}`, url: `http://127.0.0.1:${PORT}`, reuseExistingServer: false, timeout: 180000 },
});
