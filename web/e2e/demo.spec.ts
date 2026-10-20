import { test, expect, type Page, type TestInfo } from '@playwright/test';
import { mkdir } from 'node:fs/promises';

async function shot(page: Page, info: TestInfo, name: string) {
  await mkdir('.shots', { recursive: true });
  await page.screenshot({ path: `.shots/${info.project.name}-${name}.png`, fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}
/** The app lands on Today; a change is opened from the newest row in the feed. */
async function openChange(page: Page) { await page.locator('[data-change="clinic-bloods"] .feed-hit').click(); }
async function proposal(page: Page, variant = 'happy') {
  await page.goto(`/?variant=${variant}`);
  await openChange(page);
  await page.getByRole('button', { name: 'Handle this change', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Approve this change', exact: true })).toBeVisible();
}
async function nav(page: Page, label: string) { await page.getByRole('button', { name: label, exact: true }).filter({ visible: true }).first().click(); }

test('the six places and the complete hero path', async ({ page, baseURL }, info) => {
  const external: string[] = [];
  // The app's OWN origin, read from the config rather than written out again here. The literal it
  // replaces was `http://127.0.0.1:3102`, which silently stopped meaning "this app" the moment the
  // suite's port became overridable — and the assertion it guards ("nothing is fetched off this
  // machine") would then have failed on every one of the app's own files.
  const origin = new URL(baseURL!).origin;
  page.on('request', request => { if (!request.url().startsWith(origin)) external.push(request.url()); });

  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Today', level: 1 })).toBeVisible();
  await expect(page.locator('[data-change="clinic-bloods"]')).toContainText('Northgate Clinic asked to move an appointment.');
  // What this thread refused rides on its own row, not a second row for the same meeting.
  await expect(page.locator('[data-change="clinic-bloods"]')).toContainText('Also blocked');
  await shot(page, info, '00-today');

  await nav(page, 'Settings');
  await expect(page.getByRole('heading', { name: 'Private availability' })).toBeVisible();
  await shot(page, info, '01-settings');

  await nav(page, 'Today');
  await openChange(page);
  await expect(page.getByRole('button', { name: 'Handle this change' })).toBeVisible();
  await shot(page, info, '02-change-detected');

  await page.getByRole('button', { name: 'Handle this change' }).click();
  await expect(page.getByText('Preparing your plan', { exact: true })).toBeVisible();
  await shot(page, info, '03-preparing');

  await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
  // The exact actions are on screen BEFORE approval — no separate "review" click.
  await expect(page.locator('.proposed-change')).toContainText('front.desk@northgate.example.com');
  await page.locator('.how-checked > summary').click();
  await expect(page.locator('.proposed-headline')).toContainText('15:00');
  await shot(page, info, '04-change-three-columns');

  await page.getByRole('button', { name: 'Approve this change' }).click();
  await expect(page.getByText('Calendar updated', { exact: true })).toBeVisible();
  await expect(page.getByText('Reply sent', { exact: true })).toBeVisible();
  await expect(page.getByText('Simulated — no external changes were made', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Back to Today', exact: true })).toBeVisible();
  await shot(page, info, '06-outcome');

  await page.getByRole('button', { name: 'Why this plan?', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Kept private', exact: true })).toBeVisible();
  await shot(page, info, '07-why');

  await nav(page, 'Today');
  await expect(page.locator('.feed-day [data-change="clinic-bloods"]')).toContainText('Done');
  await shot(page, info, '08-today-after');

  await nav(page, 'Agreements');
  await expect(page.locator('.agreement.drifted')).toBeVisible();
  await expect(page.locator('.agreement.unchecked').first()).toBeVisible();
  await shot(page, info, '09-agreements');

  await nav(page, 'Settings');
  await nav(page, 'Preferences');
  await page.getByLabel('Fact', { exact: true }).fill('Prefer quiet cafés');
  await page.getByRole('button', { name: 'Consent and save' }).click();
  await expect(page.getByRole('heading', { name: 'Prefer quiet cafés', exact: true })).toBeVisible();
  await shot(page, info, '10-preferences');

  expect(external).toEqual([]);
});

test('stale approval stops the bundle and demands a fresh review', async ({ page }, info) => {
  await proposal(page, 'stale');
  await page.getByRole('button', { name: 'Approve this change' }).click();
  await expect(page.getByRole('button', { name: 'Approve this change' })).toBeDisabled();
  await expect(page.getByText('The plan changed. Review it again.', { exact: true })).toBeVisible();
  await expect(page.getByText('Reply sent', { exact: true })).toHaveCount(0);
  await shot(page, info, '05-stale');
});

for (const variant of ['partial', 'unknown'] as const)
  test(`${variant} keeps per-effect truth`, async ({ page }, info) => {
    await proposal(page, variant);
    await page.getByRole('button', { name: 'Approve this change' }).click();
    await expect(page.getByText('Calendar updated', { exact: true })).toBeVisible();
    await expect(page.getByText(`Email reply · ${variant === 'partial' ? 'failed' : 'unknown'}`, { exact: true })).toBeVisible();
    await expect(page.getByText('Reply sent', { exact: true })).toHaveCount(0);
    await shot(page, info, `06-${variant}`);
  });

test('first run, no-fit, cancel, edit validation, expired and responsive layouts', async ({ page }, info) => {
  await page.goto('/?variant=empty');
  await expect(page.getByRole('heading', { name: 'Set it up once.' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Teach it one rule' })).toBeVisible();
  await shot(page, info, '02-first-run');

  await page.goto('/?variant=no_safe_fit');
  await openChange(page);
  await page.getByRole('button', { name: 'Handle this change' }).click();
  await expect(page.getByRole('heading', { name: "There isn't a plan inside these boundaries.", exact: true })).toBeVisible();
  await shot(page, info, '04-no-safe-fit');

  await proposal(page);
  await page.getByRole('button', { name: 'Edit', exact: true }).click();
  await page.getByLabel('Meeting time').fill('16:00');
  await expect(page.getByLabel('Meeting time')).toHaveValue('16:00');
  await page.getByRole('button', { name: 'Save and revalidate' }).click();
  await expect(page.getByRole('alert').filter({ hasNot: page.locator('#__next-route-announcer__') }).and(page.locator('.alert'))).toContainText('protected');
  await page.getByRole('button', { name: 'Cancel edit' }).click();
  await page.getByRole('button', { name: 'Leave unchanged' }).click();
  await expect(page.getByRole('heading', { name: 'Left unchanged.' })).toBeVisible();
  await shot(page, info, '04-stopped');

  await proposal(page, 'expired');
  await expect(page.getByRole('button', { name: 'Approve this change' })).toBeDisabled();
  await shot(page, info, '05-expired');

  for (const width of [320, 768, 1280]) {
    await page.setViewportSize({ width, height: width === 320 ? 568 : 800 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await shot(page, info, `responsive-${width}`);
  }
});

test('keyboard-only hero flow and 200% equivalent reflow', async ({ page }, info) => {
  await page.goto('/');
  const activate = async (name: string) => {
    const target = page.getByRole('button', { name, exact: true }).filter({ visible: true }).first();
    for (let i = 0; i < 120; i++) {
      if (await target.evaluate(el => el === document.activeElement)) {
        await page.keyboard.press('Enter');
        return;
      }
      await page.keyboard.press('Tab');
    }
    throw new Error(`Keyboard could not reach ${name}`);
  };
  // The feed row is a button whose accessible name is the sentence it shows.
  await page.keyboard.press('Tab');
  const row = page.locator('[data-change="clinic-bloods"] .feed-hit');
  for (let i = 0; i < 120; i++) {
    if (await row.evaluate(el => el === document.activeElement)) break;
    await page.keyboard.press('Tab');
  }
  await page.keyboard.press('Enter');
  await activate('Handle this change');
  await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
  await activate('Approve this change');
  await expect(page.getByText('Reply sent', { exact: true })).toBeVisible();
  // A 1440x900 screen at 200% browser zoom exposes a 720x450 CSS layout viewport.
  await page.setViewportSize({ width: 720, height: 450 });
  await shot(page, info, 'reflow-200-percent-equivalent');
});

test('connection, long wait and offline exceptions', async ({ page, context }, info) => {
  await page.goto('/?variant=missing');
  await nav(page, 'Settings');
  await expect(page.getByRole('button', { name: 'Connect calendar' })).toBeVisible();
  await shot(page, info, '01-disconnected');
  await page.getByRole('button', { name: 'Connect calendar' }).click();
  await expect(page.getByRole('button', { name: 'Check authorization' })).toBeVisible();
  await shot(page, info, '01-authorizing');
  await expect(page.getByRole('button', { name: 'Check authorization' })).toHaveCount(0);

  await page.goto('/?variant=expired_connection');
  await nav(page, 'Settings');
  await expect(page.getByRole('button', { name: 'Reconnect gmail' })).toBeVisible();
  await shot(page, info, '01-expired-connection');

  await page.goto('/?variant=long_wait');
  await openChange(page);
  await page.getByRole('button', { name: 'Handle this change' }).click();
  await expect(page.getByText(/Still checking venue information/)).toBeVisible({ timeout: 20000 });
  await shot(page, info, '03-long-wait');
  await page.getByRole('button', { name: 'Cancel preparation' }).click();

  await context.setOffline(true);
  await expect(page.getByText('You’re offline.', { exact: true })).toBeVisible();
  await shot(page, info, '02-offline');
  await context.setOffline(false);
});

test('both licensed typefaces are served locally', async ({ page }) => {
  for (const asset of ['manrope-latin.woff2', 'Manrope-OFL.txt', 'source-sans-3-latin.ttf', 'SourceSans3-OFL.txt'])
    expect((await page.request.get(`/fonts/${asset}`)).ok(), asset).toBe(true);
});

test('teach once, inspect permission, install, reuse and revoke', async ({ page }, info) => {
  await page.goto('/?variant=happy');
  await openChange(page);
  await page.locator('.teach-fold summary').click();
  await page.getByRole('button', { name: 'Teach this routine' }).click();
  await expect(page.getByRole('button', { name: 'Review skill permissions' })).toBeVisible();
  await page.getByText('How this skill was checked · 2 attempts', { exact: true }).click();
  await expect(page.getByRole('heading', { name: 'v1 stopped' })).toBeVisible();
  await expect(page.getByText('private_context_read', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'v2 passed' })).toBeVisible();
  await shot(page, info, 'D-attempts');

  await page.getByRole('button', { name: 'Review skill permissions' }).click();
  await expect(page.getByRole('heading', { name: 'Never', exact: true })).toBeVisible();
  await shot(page, info, 'D-permission');

  await page.getByRole('button', { name: 'Install this skill' }).click();
  await expect(page.getByText('Skill installed. No invite changed and no email sent.')).toBeVisible();
  await page.getByRole('button', { name: 'Prepare with this skill' }).click();
  await page.getByRole('button', { name: 'Approve this change' }).click();
  await page.getByRole('button', { name: 'Open a later sample change' }).click();
  await page.getByRole('button', { name: 'Handle this change' }).click();
  await expect(page.locator('.reuse')).toContainText('Using your skill: Meeting option evaluator');
  await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
  await shot(page, info, 'D-reuse');

  await nav(page, 'Skills');
  await page.getByRole('button', { name: 'Revoke Meeting option evaluator' }).click();
  await expect(page.getByText('Disabled for future plans. Earlier action records remain.')).toBeVisible();
  await shot(page, info, 'D-library-revoked');
});

for (const colorScheme of ['light', 'dark'] as const) {
  test(`the refusal reads as a moment in the flow at 375px in ${colorScheme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.emulateMedia({ colorScheme });
    await page.goto('/');
    // What this thread refused rides ON its own card — not a second row for the same meeting.
    await expect(page.locator('[data-change="clinic-bloods"]')).toContainText('Northgate Clinic asked to move an appointment.');
    await expect(page.locator('[data-change="clinic-bloods"]')).toContainText('Refused: the email tried to add a recipient');
    await shot(page, info, `refusal-feed-${colorScheme}`);

    await openChange(page);
    await page.locator('.how-checked > summary').click();
    await expect(page.locator('mark')).toContainText('cc the benefits team on the confirmation, with her benefits id');
    // At 375px the three columns are three tabs; "Blocked attempt" and the decision log live in
    // the third tab ("What trusted code checked"), not the default first one.
    await page.locator('.columns-mobile-tabs').getByText('What trusted code checked', { exact: true }).click();
    await expect(page.getByRole('region', { name: 'Blocked attempt' })).toContainText('Nothing was sent.');
    const log = page.locator('.decision-log');
    await expect(log).not.toHaveAttribute('open', '');
    await expect(log.locator('summary')).toHaveText('Decision log3 allowed · 1 refused');
    await log.locator('summary').click();
    await expect(log.locator('li')).toHaveCount(4);
    await expect(log.locator('li').nth(1)).toContainText('refused');

    await page.getByRole('button', { name: 'Handle this change' }).click();
    await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
    await expect(page.locator('.proposed-change .email-body')).not.toContainText('benefits');
    await expect(page.locator('.proposed-change .email-body')).not.toContainText('home address');
    await shot(page, info, `refusal-review-${colorScheme}`);

    await page.getByRole('button', { name: 'Approve this change' }).click();
    await expect(page.getByText('Reply sent', { exact: true })).toBeVisible();
    await expect(page.getByRole('region', { name: 'Blocked attempt' })).toContainText('Nothing was sent from this blocked attempt.');
    await shot(page, info, `refusal-outcome-${colorScheme}`);
  });
}
