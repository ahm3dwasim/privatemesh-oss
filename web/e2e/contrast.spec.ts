import { test, expect, type Page } from '@playwright/test';

/**
 * WCAG AA contrast, MEASURED on the rendered page rather than argued from the token table.
 *
 * A token pair that passes in isolation can still fail in place: a chip sets a background, a card
 * sets another, and the colour a word actually sits on is the first opaque one up the tree. So this
 * walks every text node and every control border, resolves the real backdrop, and computes the
 * ratio. 4.5:1 for body text, 3:1 for large text (≥24px, or ≥18.66px bold) and for the non-text
 * boundaries of controls.
 *
 * Failures print the element, the two colours and the ratio, so the fix is a token change and not a
 * hunt.
 */
const AUDIT = `(() => {
  const srgb = (c) => { const v = c / 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const parse = (value) => {
    const m = value.match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const parts = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number);
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const over = (top, bottom) => ({
    r: top.r * top.a + bottom.r * (1 - top.a),
    g: top.g * top.a + bottom.g * (1 - top.a),
    b: top.b * top.a + bottom.b * (1 - top.a), a: 1,
  });
  const lum = (c) => 0.2126 * srgb(c.r) + 0.7152 * srgb(c.g) + 0.0722 * srgb(c.b);
  const ratio = (a, b) => { const l1 = lum(a), l2 = lum(b); const [hi, lo] = l1 > l2 ? [l1, l2] : [l2, l1]; return (hi + 0.05) / (lo + 0.05); };
  const backdrop = (el) => {
    let stack = [];
    for (let node = el; node; node = node.parentElement) {
      const bg = parse(getComputedStyle(node).backgroundColor);
      if (bg && bg.a > 0) { stack.push(bg); if (bg.a === 1) break; }
    }
    let base = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = stack.length - 1; i >= 0; i--) base = over(stack[i], base);
    return base;
  };
  const problems = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('*')) {
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) < 0.95) continue;
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) continue;
    const text = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 1);
    const bg = backdrop(el);
    if (text) {
      const fg = parse(style.color);
      const size = parseFloat(style.fontSize);
      const bold = Number(style.fontWeight) >= 700;
      const large = size >= 24 || (size >= 18.66 && bold);
      const need = large ? 3 : 4.5;
      const got = ratio(fg, bg);
      const key = style.color + '|' + JSON.stringify(bg) + '|' + size;
      if (got < need && !seen.has(key)) {
        seen.add(key);
        problems.push({ kind: 'text', sel: el.tagName + '.' + String(el.className).slice(0, 44),
          sample: el.textContent.trim().slice(0, 44), color: style.color, on: JSON.stringify(bg),
          size, need, got: Math.round(got * 100) / 100 });
      }
    }
    // The visible boundary of a control is a non-text contrast requirement of its own (1.4.11).
    if (el.matches('button, input, select, textarea') && !el.disabled) {
      const border = parse(style.borderTopColor);
      const width = parseFloat(style.borderTopWidth);
      if (border && border.a > 0 && width > 0) {
        const got = ratio(border, backdrop(el.parentElement || el));
        const key = 'b' + style.borderTopColor + '|' + JSON.stringify(backdrop(el.parentElement || el));
        if (got < 3 && !seen.has(key)) {
          seen.add(key);
          problems.push({ kind: 'control-edge', sel: el.tagName + '.' + String(el.className).slice(0, 44),
            sample: el.textContent.trim().slice(0, 30), color: style.borderTopColor,
            on: JSON.stringify(backdrop(el.parentElement || el)), size: 0, need: 3, got: Math.round(got * 100) / 100 });
        }
      }
    }
  }
  return problems;
})()`;

async function audit(page: Page, where: string) {
  // Wait for the app to be idle first. While a request is in flight the whole navigation is
  // disabled, and a disabled control is exempt from 1.4.11 — auditing mid-flight measures the
  // greyed-out state and reports a failure that does not exist.
  await page.waitForFunction(() => !document.querySelector('.rail-nav button:disabled'));
  // Then kill transitions outright for the read. `button` animates `border-color`, and a control
  // that has just gone from disabled to enabled can still report the DISABLED colour — 1.41:1 for a
  // token that is 3.68:1 at rest. `prefers-reduced-motion` was not enough: it shortens the duration
  // rather than removing the transition, and the computed value stayed at the start. What WCAG
  // asks about is the resting state, so the resting state is what this measures.
  const freeze = await page.addStyleTag({ content: '*,*::before,*::after{transition:none!important;animation:none!important}' });
  await page.evaluate(() => new Promise(requestAnimationFrame));
  const problems = await page.evaluate(AUDIT) as { sel: string; sample: string; got: number; need: number; color: string; on: string; kind: string }[];
  await freeze.evaluate(node => node.remove());
  expect(problems.map(p => `${where} · ${p.kind} · ${p.sel} · "${p.sample}" · ${p.color} on ${p.on} · ${p.got} < ${p.need}`), where).toEqual([]);
}

async function nav(page: Page, label: string) { await page.getByRole('button', { name: label, exact: true }).filter({ visible: true }).first().click(); }

for (const theme of ['light', 'dark'] as const) {
  test.describe(`contrast ${theme}`, () => {
    test.use({ colorScheme: theme, viewport: { width: 1280, height: 900 } });

    test('every screen meets AA', async ({ page }) => {
      await page.goto('/');
      await expect(page.locator('.feed-row').first()).toBeVisible();
      await audit(page, `${theme} today`);

      // The four caregiver surfaces, audited like every other screen. The refusal and the digest
      // introduce the only two new colour pairings in this pass — the refuse-tinted verdict block
      // and the protected-tinted disclosure notice — so an unaudited pass here would be a pass
      // that skipped exactly the screens the round added.
      await page.locator('[data-change="benefits-lookalike"] .feed-hit').click();
      await page.locator('.refusal-source > summary').click();
      await audit(page, `${theme} refusal`);
      await nav(page, 'Today');
      await page.locator('[data-change="digest-tuesday"] .feed-hit').click();
      await audit(page, `${theme} digest`);
      await nav(page, 'Today');
      await nav(page, 'Care');
      await audit(page, `${theme} care circle`);
      await nav(page, 'Protected facts');
      await audit(page, `${theme} protected facts`);
      await nav(page, 'Today');

      await page.locator('[data-change="clinic-bloods"] .feed-hit').click();
      await page.getByRole('button', { name: 'Handle this change' }).click();
      await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
      await page.locator('.how-checked > summary').click();
      await page.locator('.receipt-disclosure > summary').click();
      await page.locator('.decision-log summary').click();
      await audit(page, `${theme} change`);

      await page.getByRole('button', { name: 'Approve this change' }).click();
      await expect(page.getByText('Reply sent', { exact: true })).toBeVisible();
      await audit(page, `${theme} outcome`);

      await nav(page, 'Agreements');
      await expect(page.locator('.agreement.drifted')).toBeVisible();
      await page.locator('.agreement-healthy > summary').click();
      await audit(page, `${theme} agreements`);

      await nav(page, 'Skills');
      await nav(page, 'Nothing after 15:30');
      await nav(page, 'Teach this rule');
      await nav(page, 'Review skill permissions');
      await nav(page, 'Install this skill');
      await audit(page, `${theme} skills`);

      await nav(page, 'Limits');
      await page.locator('.clauses-exact-panel > summary').click();
      await nav(page, 'Act within limits');
      await audit(page, `${theme} limits`);

      await nav(page, 'Cancel');
      await nav(page, 'Settings');
      await audit(page, `${theme} settings`);

      await page.goto('/?variant=empty');
      await expect(page.getByRole('heading', { name: 'Set it up once.' })).toBeVisible();
      await audit(page, `${theme} first run`);
    });
  });
}

/**
 * Target size, measured at 375. WCAG 2.2 AA (2.5.8) sets the floor at 24×24 CSS px; this asserts the
 * floor and reports anything under 44×44, which is the size a thumb actually wants. Inline links
 * inside a sentence are exempt by the standard and are excluded here for the same reason.
 */
test.describe('target size at 375', () => {
  test.use({ viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true });

  test('no control is smaller than the AA floor', async ({ page }) => {
    const visit = async (label: string) => {
      const small = await page.evaluate(() => {
        const out: string[] = [];
        for (const el of document.querySelectorAll<HTMLElement>('button, a[href], input, select, summary')) {
          const style = getComputedStyle(el);
          if (style.display === 'none' || style.visibility === 'hidden') continue;
          const rect = el.getBoundingClientRect();
          if (!rect.width || !rect.height) continue;
          // An inline link inside a block of text is exempt (2.5.8 "inline" exception).
          if (el.tagName === 'A' && style.display.startsWith('inline')) continue;
          if (rect.width < 24 || rect.height < 24)
            out.push(`${el.tagName}.${String(el.className).slice(0, 36)} "${(el.textContent ?? '').trim().slice(0, 26)}" ${Math.round(rect.width)}x${Math.round(rect.height)}`);
        }
        return out;
      });
      expect(small, label).toEqual([]);
    };
    await page.goto('/');
    await expect(page.locator('.feed-row').first()).toBeVisible();
    await visit('today');
    await page.locator('[data-change="clinic-bloods"] .feed-hit').click();
    await page.getByRole('button', { name: 'Handle this change' }).click();
    await expect(page.getByRole('button', { name: 'Approve this change' })).toBeVisible();
    await visit('change');
    await page.getByRole('button', { name: 'Today', exact: true }).filter({ visible: true }).first().click();
    await page.locator('[data-change="benefits-lookalike"] .feed-hit').click();
    await visit('refusal');
    await page.getByRole('button', { name: 'Today', exact: true }).filter({ visible: true }).first().click();
    await page.locator('[data-change="digest-tuesday"] .feed-hit').click();
    await visit('digest');
    await page.getByRole('button', { name: 'Care', exact: true }).filter({ visible: true }).first().click();
    await visit('care circle');
    await page.getByRole('button', { name: 'Protected facts', exact: true }).filter({ visible: true }).first().click();
    await visit('protected facts');
    await page.getByRole('button', { name: 'Limits', exact: true }).filter({ visible: true }).first().click();
    await expect(page.locator('.limit-clauses > li')).toHaveCount(7);
    await visit('limits');
  });
});
