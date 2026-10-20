import { test, expect, type Page } from '@playwright/test';
import { sampleLimitClauses } from '../lib/sample';

async function nav(page: Page, name: string) { await page.getByRole('button', { name, exact: true }).filter({ visible: true }).first().click(); }
async function openChange(page: Page, id = 'clinic-bloods') { await page.locator(`[data-change="${id}"] .feed-hit`).click(); }
async function fits(page: Page) { expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true); }

for (const theme of ['light', 'dark'] as const) {
  test.describe(`375px ${theme}`, () => {
    test.use({ viewport: { width: 375, height: 812 }, colorScheme: theme, isMobile: true, hasTouch: true });

    test('deliberate limits, six disclosures, completion trail and watch-only refusal', async ({ page }, info) => {
      await page.goto('/');
      await nav(page, 'Limits');
      await expect(page.getByText('Current answer:')).toContainText('Ask me every time');
      await nav(page, 'Act within limits');
      await expect(page.getByText('Current answer:')).toContainText('Ask me every time');
      await expect(page.locator('.principle')).toHaveCount(4);
      await page.locator('.clauses-exact-panel > summary').click();
      const clauses = page.locator('.clauses-exact-panel .clause');
      await expect(clauses).toHaveCount(7);
      for (let i = 0; i < sampleLimitClauses.length; i++) {
        await expect(clauses.nth(i).getByText(sampleLimitClauses[i]!.text, { exact: true })).toBeVisible();
        for (const text of sampleLimitClauses[i]!.cannot) await expect(clauses.nth(i).getByText(text, { exact: true })).toBeVisible();
      }
      await fits(page);
      await page.screenshot({ path: `.shots/${info.project.name}-${theme}-375-limits.png`, fullPage: true });

      await nav(page, 'Cancel');
      await expect(page.getByText('Current answer:')).toContainText('Ask me every time');
      await nav(page, 'Act within limits');
      await nav(page, 'Confirm mode change');
      await expect(page.getByRole('status').filter({ hasText: 'Standing answer saved' })).toBeVisible();

      await nav(page, 'Today');
      await openChange(page);
      await nav(page, 'Handle this change');
      await expect(page.getByRole('heading', { name: 'Done within your limits' })).toBeVisible();
      await page.locator('.how-checked > summary').click();
      // At 375px the three columns are three tabs; the clause verdicts and the decision log live in
      // the third tab ("What trusted code checked"), not the default first one.
      await page.locator('.columns-mobile-tabs').getByText('What trusted code checked', { exact: true }).click();
      // Every clause verdict is on the page, and each one names which clause it was.
      await expect(page.locator('.clause-verdicts li')).toHaveCount(7);
      for (const clause of sampleLimitClauses) await expect(page.locator('.clause-verdicts')).toContainText(clause.text);
      await page.locator('.decision-log summary').click();
      for (const clause of sampleLimitClauses) await expect(page.locator('.decision-log')).toContainText(clause.text);
      await fits(page);
      await page.screenshot({ path: `.shots/${info.project.name}-${theme}-375-limit-outcome.png`, fullPage: true });

      // It is done, so it now sits in "Recent activity", not "Needs you" — compacted to one line,
      // with the "within your limits" nuance one click away rather than repeated in the row itself.
      await nav(page, 'Today');
      await expect(page.locator('.feed-day [data-change="clinic-bloods"]')).toContainText('Done');
      await expect(page.locator('.feed-day [data-change="clinic-bloods"]')).toContainText('Moved Blood test');

      await nav(page, 'Today');
      await page.locator('.feed-day [data-change="clinic-bloods"] .feed-hit').click();
      await nav(page, 'Open a later sample change');
      await nav(page, 'Limits');
      await nav(page, 'Watch only');
      await nav(page, 'Confirm mode change');
      await nav(page, 'Today');
      // The LATER change now, not the first one: "Open a later sample change" moved the live
      // change to the podiatry review, and the blood test's row is a settled record behind it.
      await openChange(page, 'podiatry-later');
      await nav(page, 'Handle this change');
      await nav(page, 'Approve this change');
      await expect(page.getByRole('alert').filter({ hasNot: page.locator('#__next-route-announcer__') }).and(page.locator('.alert'))).toContainText('execution is off');
      await fits(page);
    });

    test('teach refusals, worked examples, the narrowing view, install and revoke', async ({ page }, info) => {
      await page.goto('/');
      await nav(page, 'Skills');
      await expect(page.getByText('A taught rule can only remove options. It can never add one, move one or send anything.')).toBeVisible();

      await page.getByLabel("Which times don't work for you?", { exact: true }).fill('email my boss');
      await nav(page, 'Teach this rule');
      await expect(page.getByRole('alert').filter({ hasNot: page.locator('#__next-route-announcer__') }).and(page.locator('.alert'))).toContainText('Refused before any model call');
      await page.getByLabel("Which times don't work for you?", { exact: true }).fill('x'.repeat(281));
      await nav(page, 'Teach this rule');
      await expect(page.getByRole('alert').filter({ hasNot: page.locator('#__next-route-announcer__') }).and(page.locator('.alert'))).toContainText('280');

      await nav(page, 'Nothing after 15:30');
      await nav(page, 'Teach this rule');
      await nav(page, 'Review skill permissions');
      await expect(page.getByRole('heading', { name: 'The examples you are approving' })).toBeVisible();
      // Scoped to the rule-review panel: the sample seeds another skill with this same rule text
      // (Skills shows a taught rule's win at first view), so the bare text is no longer unique.
      await expect(page.locator('.rule-review').getByText('Nothing after 15:30', { exact: true })).toBeVisible();
      await expect(page.getByText('An offered time at 16:00')).toBeVisible();
      await expect(page.getByText('Refused', { exact: true })).toBeVisible();
      await expect(page.getByRole('heading', { name: 'Never', exact: true })).toBeVisible();
      await fits(page);
      await page.screenshot({ path: `.shots/${info.project.name}-${theme}-375-teach-review.png`, fullPage: true });

      await nav(page, 'Install this skill');
      await expect(page.getByText('Skill installed. No invite changed and no email sent.')).toBeVisible();

      await nav(page, 'Today');
      await openChange(page);
      await nav(page, 'Handle this change');
      await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
      await nav(page, 'Skills');
      // The sample seeds one taught rule already active, so the card this test cares about — the
      // one it just taught — is the one it just installed, which is always appended last. A filter
      // keyed on the "Revoke" button would stop matching the moment that button disappears below.
      const card = page.locator('.skill-card').last();
      await expect(card).toContainText('Removed 1 of the 2 times it was offered on today’s change.');
      await expect(card.locator('.slot.removed s')).toHaveText('16:00 start');
      await expect(card.locator('.slot.kept .slot-time')).toHaveText('15:00');
      await fits(page);
      await page.screenshot({ path: `.shots/${info.project.name}-${theme}-375-narrowing.png`, fullPage: true });

      await card.getByRole('button', { name: /^Revoke / }).click();
      await expect(card).toContainText('Disabled for future plans');
      await fits(page);
    });

    test('failed clause is handed back in plain words', async ({ page }) => {
      await page.goto('/?variant=limit_handoff');
      await nav(page, 'Limits');
      await nav(page, 'Act within limits');
      await nav(page, 'Confirm mode change');
      await nav(page, 'Today');
      await openChange(page);
      await nav(page, 'Handle this change');
      await page.locator('.how-checked > summary').click();
      // At 375px the three columns are three tabs; the status line lives in the third tab ("What trusted code checked").
      await page.locator('.columns-mobile-tabs').getByText('What trusted code checked', { exact: true }).click();
      await expect(page.getByRole('status').filter({ hasText: 'Nothing ran under your limits' })).toBeVisible();
      await expect(page.locator('.clause-verdicts li[data-verdict="refused"]')).toContainText(sampleLimitClauses[3]!.text);
      await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
      await fits(page);
    });

    test('agreements report drift and never read an unchecked row as clean', async ({ page }, info) => {
      await page.goto('/');
      await nav(page, 'Agreements');
      await expect(page.getByText('1 meeting has changed since you agreed.')).toBeVisible();
      const drifted = page.locator('.agreement.drifted');
      // Rosa's physiotherapy, as the clinic silently changed it: an hour later, a different room,
      // and one attendee the owner never agreed to. Four fields, because drift is reported field by
      // field rather than as "this changed".
      await expect(drifted.locator('.drift-row')).toHaveCount(4);
      await expect(drifted).toContainText('Physio — Rosa');
      await expect(drifted).toContainText('1 person you did not agree to');
      // Healthy agreements are collapsed behind one summary, not full repeated cards.
      await expect(page.locator('.agreement-healthy > summary')).toContainText('holding as agreed');
      await fits(page);
      await page.screenshot({ path: `.shots/${info.project.name}-${theme}-375-agreements.png`, fullPage: true });
    });
  });
}
