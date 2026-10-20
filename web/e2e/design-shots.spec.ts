import { test, expect, type Page } from '@playwright/test';
import { mkdir } from 'node:fs/promises';

/**
 * Every screen, at both judged widths, in both themes, written to `web/design-shots/`.
 *
 * These are the artifacts the blind critique round reads. They are deliberately NOT assertions about
 * how the pages look — a screenshot test that also judges its own output is the rubber stamp the
 * quality method exists to avoid. The only thing asserted here is the one thing a picture cannot
 * show: that the page never scrolls sideways at the width it was captured at. The nav-overlap
 * assertion — the one thing THIS pass was told to prove for real — lives in its own test below,
 * measured in a real, non-full-page viewport rather than inferred from a picture.
 */
const widths = [
  { name: '375', width: 375, height: 812 },
  { name: '1280', width: 1280, height: 900 },
];

async function nav(page: Page, label: string) { await page.getByRole('button', { name: label, exact: true }).filter({ visible: true }).first().click(); }
/** The LIVE change's own row, by id. A week of a caregiver's appointments has several rows that
 *  need the owner, and "the first one" names none of them. */
async function openChange(page: Page) { await page.locator('[data-change="clinic-bloods"] .feed-hit').click(); }

for (const theme of ['light', 'dark'] as const) {
  for (const size of widths) {
    test.describe(`${size.name} ${theme}`, () => {
      // Deliberately NOT `isMobile`. Under mobile emulation a full-page capture enlarges the
      // emulated viewport and never restores `window.innerHeight`, so every later click is computed
      // against a viewport that no longer exists. The layout at 375 is identical either way — the
      // page carries `width=device-width` — and touch behaviour is covered by the phone project in
      // `demo.spec.ts` and `limits-teach.spec.ts`.
      test.use({ viewport: { width: size.width, height: size.height }, colorScheme: theme });

      test('every screen', async ({ page }) => {
        await mkdir('design-shots', { recursive: true });
        // Scroll home BEFORE capturing. Chromium's full-page capture enlarges the viewport, so a
        // sticky rail renders correctly from the top of the image — but only if the page was at the
        // top when the capture began; from a scrolled position it paints the chrome into the middle
        // of the image and libels the layout. Restoring the viewport afterwards keeps the next
        // click's hit-testing honest.
        const shot = async (name: string) => {
          await page.evaluate(() => window.scrollTo(0, 0));
          await page.evaluate(() => new Promise(requestAnimationFrame));
          expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `${name} fits ${size.name}`).toBe(true);
          await page.screenshot({ path: `design-shots/${name}-${size.name}-${theme}.png`, fullPage: true });
          await page.setViewportSize({ width: size.width, height: size.height });
          await page.evaluate(() => new Promise(requestAnimationFrame));
        };

        await page.goto('/');
        await expect(page.locator('.feed-row').first()).toBeVisible();
        await shot('01-today');

        // The shot the video pauses on, and the fewest clicks it can be reached in: one, from the
        // screen the app opens on.
        await page.locator('[data-change="benefits-lookalike"] .feed-hit').click();
        await expect(page.getByRole('heading', { name: 'Refused. Nothing about Rosa left this machine.' })).toBeVisible();
        await shot('01b-refusal');
        await page.locator('.refusal-source > summary').click();
        await shot('01c-refusal-email');
        await nav(page, 'Today');

        await page.locator('[data-change="digest-tuesday"] .feed-hit').click();
        await expect(page.getByRole('heading', { name: 'Today’s family digest', level: 1 })).toBeVisible();
        await shot('01d-digest');
        await nav(page, 'Today');

        await nav(page, 'Care');
        await expect(page.locator('.circle-member')).toHaveCount(3);
        await shot('01e-care-circle');
        await nav(page, 'Protected facts');
        await expect(page.locator('.fact-row')).toHaveCount(4);
        await shot('01f-protected-facts');
        await nav(page, 'Today');

        await openChange(page);
        await expect(page.getByRole('button', { name: 'Handle this change' })).toBeVisible();
        await shot('02-change-detected');

        await page.getByRole('button', { name: 'Handle this change' }).click();
        await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
        await shot('03a-decision-first');

        await page.locator('.how-checked > summary').click();
        await shot('03b-change-three-columns');
        // At 375 the three columns are three tabs, and "What the model proposed" is the second one;
        // at 1280 all three already show side by side and the tab bar is not rendered.
        const proposalTab = page.locator('.columns-mobile-tabs').getByText('Stored sample proposal', { exact: true });
        if (await proposalTab.isVisible()) await proposalTab.click();
        await page.locator('.receipt-disclosure > summary').click();
        await expect(page.getByRole('heading', { name: 'Never left this machine' })).toBeVisible();
        await shot('04-model-receipt');

        await page.getByRole('button', { name: 'Approve this change' }).click();
        await expect(page.getByText('Reply sent', { exact: true })).toBeVisible();
        await shot('06-outcome');

        await nav(page, 'Today');
        await expect(page.locator('.feed-day [data-change="clinic-bloods"]')).toContainText('Done');
        await shot('07-today-after');

        await nav(page, 'Agreements');
        await expect(page.locator('.agreement.drifted')).toBeVisible();
        await shot('08-agreements');

        await nav(page, 'Skills');
        await nav(page, 'Nothing after 15:30');
        await nav(page, 'Teach this rule');
        await expect(page.getByRole('button', { name: 'Review skill permissions' })).toBeVisible();
        await shot('09-skills-teach');

        await nav(page, 'Review skill permissions');
        await expect(page.getByRole('heading', { name: 'The examples you are approving' })).toBeVisible();
        await shot('10-skills-examples');

        await nav(page, 'Install this skill');
        await nav(page, 'Today');
        // It just finished, so it now sits at the top of "Recent activity", not "Needs you".
        await page.locator('.feed-day [data-change="clinic-bloods"] .feed-hit').click();
        await nav(page, 'Open a later sample change');
        await nav(page, 'Handle this change');
        await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
        await nav(page, 'Skills');
        await expect(page.locator('.skill-card').first()).toContainText('Removed 1 of the 2 times');
        await shot('11-skills-narrowing');

        await nav(page, 'Limits');
        // Seven exact clauses summarised as four principles, collapsed by default.
        await expect(page.locator('.principle')).toHaveCount(4);
        await shot('12-limits');
        await page.locator('.clauses-exact-panel > summary').click();
        await expect(page.locator(String.raw`.clauses-exact-panel .clause`)).toHaveCount(7);
        await shot('12b-limits-clauses');

        await nav(page, 'Settings');
        await expect(page.getByRole('heading', { name: 'Private availability' })).toBeVisible();
        await shot('13-settings');

        await page.goto('/?variant=empty');
        await expect(page.getByRole('heading', { name: 'Set it up once.' })).toBeVisible();
        await shot('14-first-run');

        await page.goto('/?variant=limit_handoff');
        await nav(page, 'Limits');
        await nav(page, 'Act within limits');
        await nav(page, 'Confirm mode change');
        await nav(page, 'Today');
        await openChange(page);
        await nav(page, 'Handle this change');
        await page.locator('.how-checked > summary').click();
        const allowedTab = page.locator('.columns-mobile-tabs').getByText('What trusted code checked', { exact: true });
        if (await allowedTab.isVisible()) await allowedTab.click();
        await expect(page.getByRole('status').filter({ hasText: 'Nothing ran under your limits' })).toBeVisible();
        await shot('15-limit-handback');
      });
    });
  }
}

/**
 * The bottom nav must never overlap content — checked for real, in a live viewport (not a full-page
 * capture, which paints a `position: fixed` element at whatever scroll position the capture began
 * at and can itself LOOK like an overlap that never happens on a real device). This scrolls each
 * screen all the way down and measures the actual rendered boxes.
 */
test.describe('bottom nav never overlaps content at 375×812', () => {
  test.use({ viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true });

  async function assertClear(page: Page, label: string) {
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.evaluate(() => new Promise(requestAnimationFrame));
    const gap = await page.evaluate(() => {
      const nav = document.querySelector('.tabbar');
      const cards = [...document.querySelectorAll('main.canvas > *, main.canvas .card, main.canvas .feed-row, main.canvas .decision-bar')];
      const last = cards.at(-1);
      if (!nav || !last) return null;
      const navTop = nav.getBoundingClientRect().top;
      const lastBottom = last.getBoundingClientRect().bottom;
      return { navTop, lastBottom, clear: navTop - lastBottom };
    });
    expect(gap, `${label}: nav vs last card`).not.toBeNull();
    expect(gap!.clear, `${label}: last card bottom (${gap!.lastBottom}) must be above nav top (${gap!.navTop})`).toBeGreaterThanOrEqual(0);
  }

  test('Today, a long Change page, Agreements, Skills, Limits and First run', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('.feed-row').first()).toBeVisible();
    await assertClear(page, 'Today');

    await page.locator('[data-change="clinic-bloods"] .feed-hit').click();
    await page.getByRole('button', { name: 'Handle this change' }).click();
    await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
    await page.locator('.how-checked > summary').click();
    // At 375 the three columns are three tabs, one at a time — check each at its own fullest
    // expansion, since the nav could clear a short one and still overlap the tallest.
    await assertClear(page, 'Change (Request tab)');
    await page.locator('.columns-mobile-tabs').getByText('Stored sample proposal', { exact: true }).click();
    await page.locator('.receipt-disclosure > summary').click();
    await assertClear(page, 'Change (Proposal tab, expanded)');
    await page.locator('.columns-mobile-tabs').getByText('What trusted code checked', { exact: true }).click();
    await page.locator('.decision-log summary').click();
    await assertClear(page, 'Change (Allowed tab, expanded)');

    await nav(page, 'Today');
    await page.locator('[data-change="benefits-lookalike"] .feed-hit').click();
    await page.locator('.refusal-source > summary').click();
    await assertClear(page, 'Refusal (email expanded)');

    await nav(page, 'Today');
    await page.locator('[data-change="digest-tuesday"] .feed-hit').click();
    await assertClear(page, 'Digest');

    await nav(page, 'Care');
    await assertClear(page, 'Care circle');
    await nav(page, 'Protected facts');
    await assertClear(page, 'Protected facts');

    await nav(page, 'Agreements');
    await expect(page.locator('.agreement.drifted')).toBeVisible();
    await assertClear(page, 'Agreements');

    await nav(page, 'Skills');
    await assertClear(page, 'Skills');

    await nav(page, 'Limits');
    await page.locator('.clauses-exact-panel > summary').click();
    await assertClear(page, 'Limits (expanded)');

    await page.goto('/?variant=empty');
    await expect(page.getByRole('heading', { name: 'Set it up once.' })).toBeVisible();
    await assertClear(page, 'First run');
  });

  /** The decision actions must never paint over the proposal above them — checked at the very first
   *  render, with no scrolling at all, which is exactly when a bottom-anchored sticky bar following a
   *  tall proposal used to jump up and cover it (see the note on `.decision-bar` in `globals.css`). */
  test('the decision actions never cover the proposal above them, unscrolled', async ({ page }) => {
    await page.goto('/');
    await page.locator('[data-change="clinic-bloods"] .feed-hit').click();
    await page.getByRole('button', { name: 'Handle this change' }).click();
    await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
    const overlap = await page.evaluate(() => {
      const proposed = document.querySelector('.proposed-change');
      const bar = document.querySelector('.decision-bar');
      if (!proposed || !bar) return null;
      const proposedBottom = proposed.getBoundingClientRect().bottom;
      const barTop = bar.getBoundingClientRect().top;
      return { proposedBottom, barTop, gap: barTop - proposedBottom };
    });
    expect(overlap).not.toBeNull();
    expect(overlap!.gap, `decision bar top (${overlap!.barTop}) must be at or below the proposal's bottom (${overlap!.proposedBottom})`).toBeGreaterThanOrEqual(0);
  });
});

/**
 * What a person actually sees on a phone: real 375×812 viewport screenshots, never full-page. A
 * full-page capture enlarges the emulated viewport to the page's whole scroll height before shooting,
 * which paints every `position: fixed` element (the tab bar) at whatever scroll position the capture
 * began at — a place it is never actually seen from on a real device. These are taken at the top of
 * each screen and again after scrolling, the two states a reviewer needs to judge fixed and sticky
 * elements honestly.
 */
test.describe('phone screens as a person actually sees them (real viewport, not full-page)', () => {
  test.use({ viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true });

  async function shot(page: Page, name: string) {
    await mkdir('design-shots/phone-viewport', { recursive: true });
    await page.evaluate(() => new Promise(requestAnimationFrame));
    await page.screenshot({ path: `design-shots/phone-viewport/${name}.png` });
  }

  test('Today, a Change page, Outcome and Agreements — top and scrolled', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('.feed-row').first()).toBeVisible();
    await shot(page, '01-today-top');
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await shot(page, '02-today-scrolled');

    await page.evaluate(() => window.scrollTo(0, 0));
    await page.locator('[data-change="clinic-bloods"] .feed-hit').click();
    await page.getByRole('button', { name: 'Handle this change' }).click();
    await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
    await shot(page, '03-decision-top');
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await shot(page, '04-decision-scrolled');

    await page.evaluate(() => window.scrollTo(0, 0));
    await page.getByRole('button', { name: 'Approve this change' }).click();
    await expect(page.getByText('Reply sent', { exact: true })).toBeVisible();
    await shot(page, '05-outcome-top');

    await nav(page, 'Today');
    await page.locator('[data-change="benefits-lookalike"] .feed-hit').click();
    await shot(page, '06a-refusal-top');
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await shot(page, '06b-refusal-scrolled');
    await page.evaluate(() => window.scrollTo(0, 0));
    await nav(page, 'Today');
    await page.locator('[data-change="digest-tuesday"] .feed-hit').click();
    await shot(page, '06c-digest-top');
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await shot(page, '06d-digest-scrolled');
    await page.evaluate(() => window.scrollTo(0, 0));
    await nav(page, 'Care');
    await shot(page, '06e-care-circle-top');
    await nav(page, 'Protected facts');
    await shot(page, '06f-protected-facts-top');

    await nav(page, 'Agreements');
    await expect(page.locator('.agreement.drifted')).toBeVisible();
    await shot(page, '06-agreements-top');
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await shot(page, '07-agreements-scrolled');
  });
});
