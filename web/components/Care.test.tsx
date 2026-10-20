import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import Home from '@/app/page';
import { api } from '@/lib/api';
import { artifactId } from '@/lib/care';
import { DIGEST_FRAGMENTS, DIGEST_RECIPIENTS, resetSample, sampleProtectedFacts, SAMPLE_CHANGE_ID } from '@/lib/sample';

/**
 * The caregiver screens, tested for the sentence each one exists to make true rather than for the
 * elements it happens to contain.
 */
async function renderApp(variant = 'happy') {
  window.history.replaceState({}, '', `/?variant=${variant}`);
  await act(async () => { render(<Home />); });
}
async function click(name: string | RegExp) { await act(async () => { fireEvent.click(screen.getAllByRole('button', { name })[0]!); }); }
async function clickSelector(selector: string) {
  const node = document.querySelector(selector);
  if (!node) throw new Error(`No element matched ${selector}`);
  await act(async () => { fireEvent.click(node); });
}
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
async function openCare(tab: 'Care circle' | 'Protected facts' = 'Care circle') {
  await renderApp();
  await click('Care');
  await click(tab);
}
beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal('scrollTo', vi.fn()); resetSample(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

/* ---------------- the care circle ---------------- */

test('the grant is per person, and the list says so on every row rather than once at the top', async () => {
  await openCare();
  const rows = [...document.querySelectorAll('.circle-member')];
  expect(rows).toHaveLength(3);
  const rosa = rows.find(row => row.textContent?.includes('Rosa Almeida'))! as HTMLElement;
  const tom = rows.find(row => row.textContent?.includes('Tom Almeida'))! as HTMLElement;
  const clinic = rows.find(row => row.textContent?.includes('Northgate Clinic front desk'))! as HTMLElement;
  // The asymmetry is the feature: a circle where everyone may receive everything is an address book.
  expect(within(rosa).getByText('The family digest')).toBeInTheDocument();
  expect(within(rosa).getByText('Replies about care')).toBeInTheDocument();
  expect(within(tom).getByText('The family digest')).toBeInTheDocument();
  expect(within(tom).queryByText('Replies about care')).not.toBeInTheDocument();
  expect(within(clinic).getByText('Scheduling only')).toBeInTheDocument();
  expect(within(clinic).queryByText('The family digest')).not.toBeInTheDocument();
});

test('“only the owner can add a member” is a column on every row, not a sentence in the copy', async () => {
  await openCare();
  const cells = [...document.querySelectorAll('.cm-added-by')];
  // Every row, always the same answer. `POST /v2/care-circle` has no `added_by` on its body, so
  // this cell has exactly one value it can ever hold — which is what makes it evidence rather
  // than a log. A row that said anything else would mean something other than the owner had put
  // somebody on the list.
  expect(cells).toHaveLength(3);
  expect(cells.every(cell => cell.textContent === 'You')).toBe(true);
  // And the claim itself is on the page as a property, above the list.
  expect(screen.getByRole('heading', { name: 'Only you can add someone to this circle.' })).toBeInTheDocument();
  expect(screen.getByText('a member added by an email, a model answer or a taught skill')).toBeInTheDocument();
});

test('the owner adds a member through the real route, and the server writes who did it', async () => {
  await openCare();
  fireEvent.change(screen.getByLabelText('Their name'), { target: { value: 'Ana Ferreira' } });
  fireEvent.change(screen.getByLabelText('Their email'), { target: { value: 'ana.ferreira@example.com' } });
  fireEvent.click(screen.getByLabelText(/The family digest/));
  await click('Add to the circle');
  const added = (await api.careCircle!()).find(member => member.email === 'ana.ferreira@example.com')!;
  expect(added.may_receive).toEqual(['digest']);
  // Written by the server, not sent by this client.
  expect(added.added_by).toBe('owner');
  const row = [...document.querySelectorAll('.circle-member')].find(node => node.textContent?.includes('Ana Ferreira'))!;
  expect(within(row as HTMLElement).getByText('You')).toBeInTheDocument();
});

test('“on my list, trusted with nothing yet” is a state the owner is allowed to be in', async () => {
  await openCare();
  fireEvent.change(screen.getByLabelText('Their name'), { target: { value: 'Ivan Costa' } });
  fireEvent.change(screen.getByLabelText('Their email'), { target: { value: 'ivan.costa@example.com' } });
  await click('Add to the circle');
  const row = [...document.querySelectorAll('.circle-member')].find(node => node.textContent?.includes('Ivan Costa'))!;
  // Drawn as a value, not as a gap — a default here would be the product choosing a permission.
  expect(within(row as HTMLElement).getByText('Nothing yet')).toBeInTheDocument();
  expect((await api.careCircle!()).find(m => m.email === 'ivan.costa@example.com')!.may_receive).toEqual([]);
});

test('a malformed address is refused with the reason, and nothing is added', async () => {
  await openCare();
  fireEvent.change(screen.getByLabelText('Their name'), { target: { value: 'Nobody' } });
  fireEvent.change(screen.getByLabelText('Their email'), { target: { value: 'not-an-address' } });
  // Submitted past the browser's own `type="email"` check on purpose: the point of this test is
  // the SERVER's refusal, which is the one that still runs when the field is bypassed.
  await act(async () => { fireEvent.submit(document.querySelector('.care-add')!); });
  expect(screen.getByRole('alert')).toHaveTextContent('That is not an address this build can send to.');
  expect(await api.careCircle!()).toHaveLength(3);
});

test('removing a member is confirmed, and says what it does not undo', async () => {
  await openCare();
  const tom = [...document.querySelectorAll('.circle-member')].find(node => node.textContent?.includes('Tom Almeida'))! as HTMLElement;
  await act(async () => { fireEvent.click(within(tom).getByRole('button', { name: 'Remove' })); });
  expect(screen.getByText('They stop receiving anything from now on. It does not unsend what already went out.')).toBeInTheDocument();
  await click('Remove Tom');
  expect((await api.careCircle!()).some(member => member.name === 'Tom Almeida')).toBe(false);
});

/* ---------------- protected facts ---------------- */

test('a protected fact is a label, a kind and a placeholder — and there is nowhere a value could go', async () => {
  await openCare('Protected facts');
  const rows = [...document.querySelectorAll('.fact-row')];
  expect(rows).toHaveLength(4);
  expect(screen.getByRole('heading', { name: 'Benefits id' })).toBeInTheDocument();
  expect(screen.getAllByText('[[private:benefits_id]]').length).toBeGreaterThan(0);
  // The four sample values exist in `src/privatemesh/sim.py` and are distinctive on purpose. None
  // of them is anywhere in this document, because no route serves one and no row has a field for
  // one — the page cannot display what it was never given.
  for (const value of ['QQ-4471-ALPHA-9', 'POA-2024-88317-LK', '14 March 1943'])
    expect(document.body.textContent).not.toContain(value);
  // `in_use` is the masker's own record, so it distinguishes two facts rather than badging all four.
  expect(document.querySelectorAll('.in-use')).toHaveLength(sampleProtectedFacts().filter(fact => fact.in_use).length);
});

test('adding a fact takes a value once and the value is never rendered afterwards', async () => {
  await openCare('Protected facts');
  fireEvent.change(screen.getByLabelText('What to call it'), { target: { value: 'NHS number' } });
  fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'account' } });
  fireEvent.change(screen.getByLabelText('The value'), { target: { value: '943-118-2261' } });
  await click('Protect it');
  const row = [...document.querySelectorAll('.fact-row')].find(node => node.textContent?.includes('NHS number'))!;
  expect(within(row as HTMLElement).getByText('Account number')).toBeInTheDocument();
  expect(within(row as HTMLElement).getByText('[[private:account]]')).toBeInTheDocument();
  // Not in the row, not in the form field it was typed into, not anywhere on the page.
  expect(document.body.textContent).not.toContain('943-118-2261');
  expect((screen.getByLabelText('The value') as HTMLInputElement).value).toBe('');
  // And the row the API answered with has no value on it at all.
  const stored = (await api.protectedFacts!()).find(fact => fact.label === 'NHS number')!;
  expect(Object.keys(stored).sort()).toEqual(['id', 'in_use', 'kind', 'label']);
});

test('a value too short to mask is refused, because masking it would redact the day', async () => {
  await openCare('Protected facts');
  fireEvent.change(screen.getByLabelText('What to call it'), { target: { value: 'Initials' } });
  fireEvent.change(screen.getByLabelText('The value'), { target: { value: 'RA' } });
  await click('Protect it');
  expect(screen.getByRole('alert')).toHaveTextContent('too short to mask safely');
  expect(await api.protectedFacts!()).toHaveLength(4);
});

/* ---------------- the family digest ---------------- */

async function openDigest() {
  await renderApp();
  await clickSelector('[data-change="digest-tuesday"] .feed-hit');
}

test('the digest renders its fragments in order, with the record each one came from', async () => {
  await openDigest();
  const lines = [...document.querySelectorAll('.digest-line .digest-text')].map(node => node.textContent);
  expect(lines).toEqual(DIGEST_FRAGMENTS.map(fragment => fragment.text));
  expect(document.querySelector('.digest-line')).toHaveTextContent('the composer’s own opening line');
  expect(screen.getByText('from evt-physio')).toBeInTheDocument();
});

test('a fragment carrying a protected value gets the owner-facing disclosure notice', async () => {
  await openDigest();
  const notice = document.querySelector('.disclosure-notice')!;
  expect(notice).toHaveTextContent('This digest discloses a protected fact.');
  expect(notice).toHaveTextContent('Home address');
  // The value IS shown here, marked where it sits, and this is the only place in the product that
  // is true: the owner cannot approve bytes they have not been shown.
  expect(document.querySelector('mark.protected-span')).toHaveTextContent('Flat 3, 118 Wentworth Road, London N17 6XQ');
  // Both recipients are on the circle and both are listed for `digest`, so it is approvable.
  expect(notice.className).not.toContain('disclosure-blocked');
  expect(screen.getByRole('button', { name: 'Approve and send' })).toBeEnabled();
});

test('the artifact id on screen is recomputed from the bytes on screen', async () => {
  await openDigest();
  const shown = document.querySelector('.artifact-id')!.textContent;
  expect(shown).toBe(artifactId({ day: '2026-09-22', recipients: DIGEST_RECIPIENTS, fragments: DIGEST_FRAGMENTS }));
  // It agrees with the composer's own name for the same bytes, so no disagreement is reported.
  expect(screen.queryByText(/This is not the artifact the server named/)).not.toBeInTheDocument();
});

test('editing the digest changes its name, and an approval of the old bytes stops matching', async () => {
  await openDigest();
  const before = document.querySelector('.artifact-id')!.textContent!;
  // Leaving the line with the protected value out is the edit a person would actually make.
  const disclosing = [...document.querySelectorAll('.digest-line')].find(node => node.className.includes('discloses'))!;
  await act(async () => { fireEvent.click(within(disclosing as HTMLElement).getByRole('button', { name: 'Leave this line out' })); });
  const after = document.querySelector('.artifact-id')!.textContent!;
  expect(after).not.toBe(before);
  expect(after).toBe(artifactId({ day: '2026-09-22', recipients: DIGEST_RECIPIENTS, fragments: DIGEST_FRAGMENTS.filter(f => f.record_id !== 'evt-transport-thu') }));
  // The protected value went with the line, and so did the notice about it.
  expect(document.querySelector('mark.protected-span')).toBeNull();
  expect(document.querySelector('.disclosure-notice')).toBeNull();
});

test('a digest cannot be sent to somebody the owner did not list for it', async () => {
  // The circle is changed on the owner's own screen — the only place it can be — and the digest
  // screen then refuses the audience it was composed for.
  await renderApp();
  await click('Care');
  const rosa = [...document.querySelectorAll('.circle-member')].find(node => node.textContent?.includes('Rosa Almeida'))! as HTMLElement;
  await act(async () => { fireEvent.click(within(rosa).getByRole('button', { name: 'Remove' })); });
  await click('Remove Rosa');
  await click('Today');
  await clickSelector('[data-change="digest-tuesday"] .feed-hit');
  const notice = document.querySelector('.disclosure-notice')!;
  expect(notice.className).toContain('disclosure-blocked');
  expect(screen.getByText(/is not on your care circle for a digest/)).toBeInTheDocument();
});

/* ---------------- the refusal ---------------- */

test('the refusal is one click from Today, and answers what was asked, what stopped it and what got out', async () => {
  await renderApp();
  // The row itself says the outcome before anything is opened.
  const row = document.querySelector('[data-change="benefits-lookalike"]')!;
  expect(row).toHaveTextContent('Refused Benefits Update’s email. Nothing was disclosed and nothing was sent.');
  await clickSelector('[data-change="benefits-lookalike"] .feed-hit');

  expect(screen.getByRole('heading', { name: 'Refused. Nothing about Rosa left this machine.' })).toBeInTheDocument();
  const counts = [...document.querySelectorAll('.count')];
  expect(counts).toHaveLength(2);
  expect(counts[0]).toHaveTextContent('It asked for two protected facts, in a reply.');
  // The gate's own words, twice — once per fact — never a paraphrase of them.
  expect(counts[0]).toHaveTextContent('the protected Benefits id would have been disclosed to 1 recipient(s)');
  expect(counts[0]).toHaveTextContent('the protected Power of attorney reference would have been disclosed to 1 recipient(s)');
  // The second count is not drawn as a refusal, because nothing refused it: there is no path.
  expect(counts[1]).toHaveTextContent('It asked to be added to your family list.');
  expect(counts[1]).toHaveTextContent('There was no check to fail.');
  expect(counts[1]).toHaveTextContent('3 member(s) before and after, none added and none removed');

  // The measured checks, quoted.
  expect(screen.getByText(/4 protected fact\(s\), none of them in anything outbound/)).toBeInTheDocument();
  expect(screen.getAllByText(/3 message\(s\) in the inbox, 1 raised a change, 0 other change\(s\) carried/).length).toBeGreaterThan(0);

  // And the protected values themselves are nowhere on the screen that is about them.
  for (const value of ['QQ-4471-ALPHA-9', 'POA-2024-88317-LK'])
    expect(document.body.textContent).not.toContain(value);
});

test('a change that ran out of slots is not dressed as a security event', async () => {
  await renderApp();
  await clickSelector('[data-change="dentist-parkside"] .feed-hit');
  // Same terminal stage, no refusal on the log: the boundary card, not the refusal screen.
  expect(document.querySelector('.refusal')).toBeNull();
  expect(screen.getByRole('heading', { name: "There isn't a plan inside these boundaries." })).toBeInTheDocument();
});

test('a live change still reaches its plan with the care week in place', async () => {
  await renderApp();
  await clickSelector(`[data-change="${SAMPLE_CHANGE_ID}"] .feed-hit`);
  await click('Handle this change');
  await advance(2600);
  expect(screen.getByRole('button', { name: 'Approve this change' })).toBeInTheDocument();
});

test('approving the digest sends those bytes, and the receipt names the artifact it sent', async () => {
  await openDigest();
  const artifact = document.querySelector('.artifact-id')!.textContent!;
  await click('Approve and send');
  await advance(2400);
  // The receipt is about the digest, not about a calendar edit that never existed.
  expect(screen.getByText('Digest sent')).toBeInTheDocument();
  // And it names the artifact — the same one the screen computed from the bytes it showed.
  expect(document.querySelector('.outcome')).toHaveTextContent(artifact);
  expect(document.querySelector('.outcome')).toHaveTextContent('Sent to 2 care-circle member(s).');
});

test('an edited digest is a new version, so an approval of the old one cannot be used', async () => {
  await openDigest();
  const before = document.querySelector('.artifact-id')!.textContent!;
  const disclosing = [...document.querySelectorAll('.digest-line')].find(node => node.className.includes('discloses'))!;
  await act(async () => { fireEvent.click(within(disclosing as HTMLElement).getByRole('button', { name: 'Leave this line out' })); });
  const after = document.querySelector('.artifact-id')!.textContent!;
  expect(after).not.toBe(before);
  // The version moved with the bytes, so the server refuses an approval quoting the old one —
  // the belt to the artifact id's braces, and the reason the two are shown together.
  await expect(api.approve('digest-tuesday', 1)).rejects.toMatchObject({ status: 409 });
  // Approving what is now on screen still works, and it sends the edited artifact.
  await click('Approve and send');
  await advance(2400);
  expect(document.querySelector('.outcome')).toHaveTextContent(after);
});
