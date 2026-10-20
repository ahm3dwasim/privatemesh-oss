import { test, expect, type Page, type TestInfo } from '@playwright/test';
import { mkdir } from 'node:fs/promises';

import { API, GATE } from './live-env';

// The claim, driven end to end against a real backend process instead of the in-browser fixture.
//
// Everything here goes over HTTP to `privatemesh serve --v2 --sample`: the day the workspace opens
// on, the change the owner is asked to handle, the learning loop, the approval, the recorded
// outcome and the disclosure. Nothing is stubbed on the client side — `NEXT_PUBLIC_API_MODE=live`
// means `web/lib/api.ts` is a transport, and a screen that renders is a screen the backend filled.
//
// The app has six places and opens on Today, so a change is reached by clicking its row in the
// feed rather than through an entry screen. The feed itself is built from `GET /today`, `GET /plans`
// and one `GET /changes/{id}` per row, so a row that reads correctly here is a row the backend
// filled.

async function shot(page: Page, info: TestInfo, name: string) {
  await mkdir('.shots-live', { recursive: true });
  await page.screenshot({ path: `.shots-live/${info.project.name}-${name}.png`, fullPage: true });
  // A screenshot is not evidence of a layout that fits: this is the check that the page does not
  // scroll sideways at either viewport, taken at the same moment.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

/** Go where the label says, in either layout.
 *
 * The six places are in the sidebar at 1440×900 and in the bottom bar at 390×844, with the same
 * labels in both. Connections and Preferences are tabs INSIDE Settings, so those two are one click
 * on neither viewport and two on both — hence the fallback. */
async function nav(page: Page, label: string) {
  const direct = page.getByRole('button', { name: label, exact: true }).filter({ visible: true }).first();
  if (await direct.count()) {
    await direct.click();
    return;
  }
  await page.getByRole('button', { name: 'Settings', exact: true }).filter({ visible: true }).first().click();
  await page.getByRole('button', { name: label, exact: true }).filter({ visible: true }).first().click();
}

/** The open change, read from the API rather than from the screen.
 *
 * `docs/API_V2.md` says the day lists open changes first, and the spec needs the id for the one
 * call the UI has no button for: bumping the bundle version behind an open review screen, which is
 * how a stale approval is produced against a real server.
 *
 * It carries no owner token, and that is a property of THIS backend rather than a claim about the
 * product: `API` is the sample server, sample mode is open on purpose, and `docs/API_V2.md`
 * documents it that way. The last test in this file makes the difference explicit — the same read
 * against the live-mode server is a refusal — so a tokenless call here cannot be read as evidence
 * that the gate is missing. */
async function openChangeId(page: Page): Promise<string> {
  const response = await page.request.get(`${API}/v2/today`);
  expect(response.ok()).toBe(true);
  const body = await response.json();
  return body.changes[0].id;
}

/** The open change AS THE SERVER HAS IT — the version the owner is being asked to approve. */
async function liveChange(page: Page): Promise<{ id: string; version: number; stage: string }> {
  const response = await page.request.get(`${API}/v2/changes/${await openChangeId(page)}`);
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

test('the judges path: live client, sample backend, one routine taught and reused', async ({ page }, info) => {
  // An unexpected third-party request is a failure, not a detail: this build has no provider of
  // its own, so anything leaving for the internet is a bug in the client.
  const external: string[] = [];
  page.on('request', request => {
    const url = request.url();
    if (!url.startsWith(API) && !url.startsWith('http://127.0.0.1:'))
      external.push(url);
  });

  // ---- the workspace opens on the backend's day ------------------------------------------------
  await page.goto('/');
  // "Demo mode" is the BACKEND's answer, not the build's: this client was built live, and the
  // server it is pointed at says its data is synthetic. A build-time flag would have called this a
  // live workspace and dropped the indicator below. It is the ONE place this build says so — the
  // repeated page-body disclaimers pass 1 had here are gone by design (direction 2).
  await expect(page.getByText('Demo mode', { exact: true })).toBeVisible();
  // The feed is the home screen, and its first row is the change the backend says needs the owner.
  await expect(page.locator('.feed-row').first()).toBeVisible();
  await shot(page, info, '00-today');

  await page.locator('.feed-hit').first().click();
  await expect(page.getByRole('heading', { name: /’s request needs a new plan$/ })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Handle this change', exact: true })).toBeVisible();
  await shot(page, info, '01-change');

  // ---- learning: the first attempt overreaches and is stopped ----------------------------------
  await page.getByRole('button', { name: 'Handle this change', exact: true }).click();
  // No "Preparing your plan" step is asserted here, and that is a real difference between the two
  // clients rather than a missing screen: the in-browser fixture stages `reading_thread` →
  // `checking_availability` → `checking_venues` over elapsed time so the states can be seen, while
  // the backend runs them inside the one POST and answers with the finished change. The stage names
  // are in the contract and the UI polls for them; this server simply never makes the browser wait.
  await expect(page.getByRole('button', { name: 'Approve this change', exact: true })).toBeVisible();
  // The exact calendar diff and reply are shown BEFORE the approve button — no separate "review"
  // click, because reading this far already is the review.
  await expect(page.locator('.proposed-change')).toContainText('alex@');
  // The trust boundary, one click away: the untrusted email and the model's suggestion.
  await page.locator('.how-checked > summary').click();
  await expect(page.locator('.proposed-headline')).toContainText('15:00');
  await expect(page.locator('.column.untrusted')).toHaveCount(2);
  await shot(page, info, '03-proposal');

  // The trust boundary is one click away now, behind "How this was checked" — the columns above are
  // asserted on its content, which does not need it open, but the receipt disclosure inside it does.
  await page.locator('.how-checked').evaluate(el => { (el as HTMLDetailsElement).open = true; });
  // At ≤1180px the three columns are three tabs, and the receipt disclosure lives in the second
  // one ("Proposal"); on the desktop project all three already show side by side.
  const proposalTab = page.getByText('2. Proposal', { exact: true });
  if (await proposalTab.isVisible()) await proposalTab.click();
  // The model receipt is owner-token-gated. Whatever this build is allowed to read, the panel must
  // never render "nothing was sent" for a call it simply could not see.
  await page.locator('.receipt-disclosure > summary').click();
  await expect(page.locator('.receipt-disclosure')).not.toContainText('Nothing was sent');
  await shot(page, info, '03b-model-receipt');

  // "Teach it once" now sits in its own collapsed fold below the plan, not a second primary action.
  await page.locator('.teach-fold summary').click();
  await expect(page.getByRole('heading', { name: 'Teach it once. Keep control.' })).toBeVisible();
  await page.getByRole('button', { name: 'Teach this routine', exact: true }).click();
  // The loop settles on `ready_for_review`; the panel offers the permission page only then.
  await expect(page.getByRole('button', { name: 'Review skill permissions' })).toBeVisible();

  // The attempt history is the reviewer's evidence, and it has to name BOTH attempts: v1 was
  // stopped, v2 passed. A single "it passed" would hide that the first one overreached.
  await expect(page.locator('.attempt-history summary')).toContainText('2 attempts');
  await page.locator('.attempt-history summary').click();
  await expect(page.locator('.attempt-history h3').first()).toHaveText(/v1\.0\.0 stopped/);
  await expect(page.locator('.attempt-history h3').last()).toHaveText(/v1\.0\.1 passed/);
  await expect(page.locator('.attempt-history')).toContainText('urllib');
  await expect(page.locator('.attempt-history')).toContainText('Stopped ·');
  await shot(page, info, '04-learning');

  // ---- the permission page, then the install ---------------------------------------------------
  await page.getByRole('button', { name: 'Review skill permissions' }).click();
  await expect(page.getByRole('heading', { name: 'Keep this skill. Keep control.' })).toBeVisible();
  await expect(page.locator('.permission-columns')).toContainText('Reads');
  await expect(page.locator('.permission-columns')).toContainText('Never');
  // The expiry is a real instant the manifest was hashed with. `docs/API_V2.md` gives the UI an
  // Install button that refuses an artifact whose lifetime it cannot read, so "Unavailable" here
  // is a dead button, not a cosmetic detail.
  await expect(page.locator('.permission-sheet')).not.toContainText('Expires Unavailable');
  await expect(page.locator('.permission-sheet')).toContainText('Identity sha256:');
  await shot(page, info, '05-permission');

  await page.getByRole('button', { name: 'Install this skill' }).click();
  await expect(page.getByText('Skill installed. No invite changed and no email sent.')).toBeVisible();

  // ---- the plan after the install ---------------------------------------------------------------
  // A DIVERGENCE, asserted rather than papered over. `POST /v2/changes/{id}/prepare` on a change
  // that is already `proposal_ready` is idempotent in the engine (`_prepare` returns it untouched),
  // while the in-browser sample client regenerates it — so this button cannot produce a new plan
  // against this server, and no "Using your skill:" line appears here. The click is accepted and
  // the reviewed version does not move. Reuse is proven on the SECOND change below, which is where
  // `docs/API_V2.md` puts the claim ("a later change that the library can serve").
  const before = await liveChange(page);
  await page.getByRole('button', { name: 'Prepare with this skill' }).click();
  await expect(page.getByRole('button', { name: 'Approve this change', exact: true })).toBeVisible();
  await page.locator('.how-checked > summary').click();
  await expect(page.locator('.proposed-headline')).toContainText('15:00');
  expect((await liveChange(page)).version, 're-preparing a ready change is a no-op here').toBe(before.version);
  await expect(page.locator('.proposed-change')).toContainText('alex@');
  await shot(page, info, '06-after-install');

  // ---- approve, outcome ---------------------------------------------------------------------------
  // No separate "review" click: the exact bundle above IS the review, and approving applies to it.
  await page.getByRole('button', { name: 'Approve this change' }).click();
  // This backend reports `mode: 'sample'`, so the receipt says "Simulated — no external changes were
  // made" ONCE at the top and then reads exactly like a live receipt would — the fix for a page that
  // used to say "done" one line above "Simulated result", which read as the product contradicting
  // itself.
  await expect(page.getByText('Simulated — no external changes were made', { exact: true })).toBeVisible();
  await expect(page.getByText('Calendar updated', { exact: true })).toBeVisible();
  await expect(page.getByText('Reply sent', { exact: true })).toBeVisible();
  await shot(page, info, '08-outcome');

  // ---- the disclosure ------------------------------------------------------------------------
  await page.locator('.how-checked').evaluate(el => { (el as HTMLDetailsElement).open = true; });
  await page.getByRole('button', { name: 'Why this plan?', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Kept private', exact: true })).toBeVisible();
  await expect(page.locator('.settings-sheet')).toContainText('Private categories, never their contents.');
  await expect(page.getByRole('heading', { name: 'Changed', exact: true })).toBeVisible();
  // No "Your learned skill" entry here, and that is the same divergence as above rather than a
  // gap in the disclosure: the engine names the skill in `used[]` only for a change the skill
  // actually shaped, and this one was planned before the skill existed. A close-up client would
  // find no such entry either. It is asserted on the second change, which the library served.

  // Overreach is a collection the server supplies and the UI renders only when it is non-empty.
  // This path produces none — the installed evaluator agrees with the owner's own rules — so the
  // honest assertion is the agreement between the two, not a section that is not there.
  const why = await (await page.request.get(`${API}/v2/changes/${await openChangeId(page)}/why`)).json();
  if (why.overreach.length === 0)
    await expect(page.getByRole('heading', { name: 'Stopped at your boundary' })).toHaveCount(0);
  else
    await expect(page.getByRole('heading', { name: 'Stopped at your boundary' })).toBeVisible();
  await shot(page, info, '09-why');

  // ---- the library holds exactly the skill that was installed -----------------------------------
  await nav(page, 'Skills');
  await expect(page.getByRole('heading', { name: 'Learned scheduling rules' })).toBeVisible();
  await expect(page.locator('.skill-grid')).toContainText('meeting_option_evaluator');
  await expect(page.locator('.skill-grid')).toContainText('active');
  await shot(page, info, '10-skills');

  // ---- agreements: whatever the server reports, and nothing it does not ------------------------
  await nav(page, 'Agreements');
  // The route is being built by the engine lane. Either answer is correct here; inventing a list
  // when it 404s would not be.
  await expect(page.locator('.agreements-page')).toBeVisible();
  await shot(page, info, '10b-agreements');

  // ---- the care circle and the protected facts, from the SERVER's own stores -------------------
  // These two screens are the only ones in the product whose whole claim is about data the client
  // must never hold: the circle names the owner's family, and the fact list is what must never
  // leave. Reading them over HTTP from `serve --v2 --sample` is the only way to show that the
  // rendering is the server's answer rather than the fixture's — `lib/sample.ts` is not loaded in
  // this build at all.
  await nav(page, 'Care');
  await expect(page.locator('.circle-member')).toHaveCount(3);
  await expect(page.locator('.circle-member').filter({ hasText: 'Rosa Almeida' })).toContainText('The family digest');
  await expect(page.locator('.circle-member').filter({ hasText: 'Rosa Almeida' })).toContainText('Replies about care');
  // The asymmetry is the feature, and it survives the wire: the sibling may get the digest and
  // nothing else, and the clinic may be told about scheduling and nothing else.
  await expect(page.locator('.circle-member').filter({ hasText: 'Tom Almeida' })).not.toContainText('Replies about care');
  await expect(page.locator('.circle-member').filter({ hasText: 'front desk' })).toContainText('Scheduling only');
  // `added_by` is written by the SERVER. Every row, one value.
  const addedBy = await page.locator('.cm-added-by').allTextContents();
  expect(addedBy).toEqual(['You', 'You', 'You']);
  await shot(page, info, '10c-care-circle');

  await nav(page, 'Protected facts');
  await expect(page.locator('.fact-row')).toHaveCount(4);
  await expect(page.locator('.fact-list')).toContainText('Benefits id');
  await expect(page.locator('.fact-list')).toContainText('[[private:benefits_id]]');
  // The server's own sample values, chosen in `sim.py` to be distinctive. No route returns one, so
  // none of them can be on this page — asserted against the REAL server rather than against a
  // fixture that simply never held them.
  const rendered = await page.locator('body').innerText();
  for (const value of ['QQ-4471-ALPHA-9', 'POA-2024-88317-LK', '14 March 1943', 'Wentworth Road'])
    expect(rendered, `a protected value reached the page: ${value}`).not.toContain(value);
  await shot(page, info, '10d-protected-facts');

  // Adding and removing go through the real routes, and the server writes `added_by`.
  await page.getByRole('button', { name: 'Care circle', exact: true }).click();
  await page.getByLabel('Their name').fill('Ana Ferreira');
  await page.getByLabel('Their email').fill('ana.ferreira@example.com');
  await page.getByLabel('The family digest').check();
  await page.getByRole('button', { name: 'Add to the circle' }).click();
  await expect(page.locator('.circle-member')).toHaveCount(4);
  const added = page.locator('.circle-member').filter({ hasText: 'Ana Ferreira' });
  await expect(added).toContainText('The family digest');
  await expect(added).toContainText('You');
  await added.getByRole('button', { name: 'Remove' }).click();
  await added.getByRole('button', { name: 'Remove Ana' }).click();
  await expect(page.locator('.circle-member')).toHaveCount(3);

  // ---- the second change, served from the library without asking again -------------------------
  await nav(page, 'Today');
  await page.locator('.feed-hit').first().click();
  await page.getByRole('button', { name: 'Open a later sample change' }).click();
  await expect(page.getByRole('button', { name: 'Handle this change', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Handle this change', exact: true }).click();
  // The reuse line is the whole point of the second half: the library can serve this change, so
  // the learning loop is never started and "Teach this routine" is not offered at all.
  await expect(page.locator('.reuse')).toContainText('Using your skill:');
  await expect(page.getByRole('button', { name: 'Teach this routine' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Approve this change', exact: true })).toBeVisible();
  await shot(page, info, '11-second-change');

  // The disclosure names the capability that shaped THIS plan — the half of the claim the first
  // change could not show, and the reason `docs/API_V2.md` puts reuse on "a later change".
  await page.locator('.how-checked').evaluate(el => { (el as HTMLDetailsElement).open = true; });
  await page.getByRole('button', { name: 'Why this plan?', exact: true }).click();
  await expect(page.locator('.settings-sheet')).toContainText('Your learned skill');
  await expect(page.locator('.settings-sheet')).toContainText('meeting_option_evaluator');
  await shot(page, info, '12-why-reused');
  await page.getByRole('button', { name: 'Back to the plan' }).click();

  // ---- a stale approval is refused --------------------------------------------------------------
  const secondId = await openChangeId(page);
  const version = (await (await page.request.get(`${API}/v2/changes/${secondId}`)).json()).version;
  // Move the bundle on behind the open screen, exactly as a real concurrent edit would. The edit
  // is an owner edit through the same endpoint the edit form uses, so nothing here is a fixture.
  const edited = await page.request.post(`${API}/v2/changes/${secondId}/edit`, { data: { venue_index: 1 } });
  expect(edited.ok(), await edited.text()).toBe(true);
  expect((await edited.json()).version).toBeGreaterThan(version);

  await page.getByRole('button', { name: 'Approve this change' }).click();
  await expect(page.getByText('The plan changed. Review it again.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Approve this change' })).toBeDisabled();
  await shot(page, info, '13-stale');

  expect(external).toEqual([]);
});

test('the day this journey reads is refused when the same server is live', async ({ page }) => {
  // Every read in this file goes out without a token, and up to here that has been fine — `API` is
  // the sample backend and sample mode is open by design. The risk is the sentence a reader takes
  // away from it: "a private route answers anyone", which is the fourth review's Critical. So the
  // same call is made against the LIVE-mode server, where the answer is the opposite one.
  const refused = await page.request.get(`${GATE}/v2/today`, { failOnStatusCode: false });
  expect(refused.status(), 'a live server answered a caller with no owner token').toBe(403);
  expect(((await refused.json()) as { code?: string }).code).toBe('owner_forbidden');

  // The tokenless read above is a property of the backend this suite points at, not of the product.
  // Asserted side by side with the refusal so the pair is what the file says, and so a sample server
  // that quietly started refusing would fail here rather than silently change what `openChangeId`
  // means.
  const sample = await page.request.get(`${API}/v2/today`, { failOnStatusCode: false });
  expect(sample.status(), 'the sample backend is open on purpose and must stay open').toBe(200);
});
