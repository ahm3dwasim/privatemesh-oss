import { test, expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

/**
 * The tour's own axe pass, over the two states a hands-on blind judge found real violations in:
 * the change detail's "How this was checked" (the `columns-mobile-tabs` tablist, and the
 * `DemoApp.tsx`-adjacent heading order — this file's own is `ChangeDetail.tsx`) and Agreements
 * (`heading-order`). Both projects run this (`playwright.config.ts`'s desktop and phone), since the
 * tablist violation was phone-only — the mobile tabs only render under the 1180px breakpoint.
 */

async function nav(page: Page, label: string) { await page.getByRole('button', { name: label, exact: true }).filter({ visible: true }).first().click(); }

async function audit(page: Page) {
  return new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice']).analyze();
}

test('the change detail — "How this was checked" — has no axe violations', async ({ page }) => {
  await page.goto('/');
  await page.locator('[data-change="clinic-bloods"] .feed-hit').click();
  await page.getByRole('button', { name: 'Handle this change', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
  await page.locator('.how-checked > summary').click();
  await expect(page.locator('.columns').first()).toBeVisible();
  const results = await audit(page);
  expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
});

test('agreements has no axe violations', async ({ page }) => {
  await page.goto('/');
  await nav(page, 'Agreements');
  await expect(page.locator('.agreement.drifted')).toBeVisible();
  const results = await audit(page);
  expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
});
