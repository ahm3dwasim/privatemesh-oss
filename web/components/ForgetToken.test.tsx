import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';

/**
 * Forgetting the owner credential, in the mode where forgetting it means something.
 *
 * The sixth review's Medium: `Forget token` cleared the token and then called `load` — and in live
 * mode a tokenless `api.session()` is REFUSED, so `load` rejected on its first line and every `set*`
 * under it never ran. The prior day, the limits and the owner's own controls stayed on screen, drawn
 * from memory, under a form that now said no token was held.
 *
 * That is why this file builds a LIVE client rather than rendering the sample. The sample adapter
 * answers a tokenless read, so a sample-mode version of this test would pass against the broken code
 * — the rejection that causes the bug only happens when a real server is refusing. So: `apiMode` is
 * stubbed to 'live', `fetch` is a stub that refuses exactly the way the server does (403 with
 * `owner_forbidden` when the header is absent), and the assertions are about what is left on screen.
 *
 * The stub is deliberately the coarse part: it answers a handful of GETs with fixed bodies and
 * refuses everything else. A test whose subject is "does the screen keep showing what the server
 * said" does not need a backend, and one that needed a fuller one would be testing the fuller one.
 */

const TOKEN = 'e2e-owner-token-not-a-real-credential';
/** The owner's own address, which the server reports as a connection detail. It is the private fact
 *  the connections list is built from, and the one this file follows from screen to screen. */
const OWNER_ADDRESS = 'owner@example.test';
const OWNER_NAME = 'A. Owner';
const LOADED_BOUNDARY = '17:15';
/** A name on the owner's care circle — private in the same sense the boundary and the connections
 *  are: it is family data the server only answers to the owner's own bearer token. */
const CARE_MEMBER_NAME = 'Priya Care';

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

/** Wait for the chained reads `load` makes to settle. `advanceTimersByTimeAsync` flushes microtasks
 *  as it goes, which is what the awaits inside `load` need. */
async function settle(rounds = 8) {
  for (let i = 0; i < rounds; i += 1)
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
}

function backend(url: string, init?: RequestInit): Promise<Response> {
  const path = new URL(url, window.location.origin).pathname;
  const headers = new Headers(init?.headers);
  // The gate, reproduced: no bearer, no answer. This is the one behaviour the whole file turns on,
  // so it is enforced before the route table rather than inside it.
  if (headers.get('Authorization') !== `Bearer ${TOKEN}`)
    return Promise.resolve(json(403, { code: 'owner_forbidden', detail: 'This route belongs to the owner.' }));
  const routes: Record<string, unknown> = {
    '/v2/session': {
      mode: 'live', authenticated_owner: true, owner_name: OWNER_NAME, timezone: 'Europe/London',
      connections: [
        { service: 'gmail', state: 'connected', detail: OWNER_ADDRESS },
        { service: 'calendar', state: 'connected', detail: 'Work Calendar' },
      ],
      routine: { enabled: true, paused: false, last_checked_at: '2026-09-19T09:00:00+01:00' },
      boundary: { start: LOADED_BOUNDARY, buffer_minutes: 25, configured: true },
    },
    '/v2/today': {
      date: '2026-09-19', timezone: 'Europe/London', events: [], protected: [],
      changes: [{ id: 'ch-1', from_name: 'Alex', from_address: 'alex@example.test',
        received_at: '2026-09-19T09:05:00+01:00', excerpt: 'Can we push to later in the afternoon?',
        thread_url: null, stage: 'detected' }],
    },
    '/v2/plans': [{ id: 'ch-1', title: 'Move the catch-up', state: 'needs_you', detail: 'Waiting on you.' }],
    '/v2/changes/ch-1': { executed_by: null, id: 'ch-1', version: 1, stage: 'detected',
      request_excerpt: 'Can we push to later in the afternoon?', proposal: null, blocked_reason: null,
      blocked_attempts: 0, updated_at: '2026-09-19T09:05:00+01:00' },
    '/v2/changes/ch-1/decisions': [],
    '/v2/limits': { mode: 'ask_always', clauses: [], updated_at: '2026-09-01T00:00:00+01:00' },
    '/v2/care-circle': { members: [{ id: 'cm-1', name: CARE_MEMBER_NAME, email: 'priya@example.test',
      may_receive: ['digest'], added_by: 'owner', added_at: '2026-09-19T09:00:00+01:00' }] },
    '/v2/protected-facts': [],
  };
  const body = routes[path];
  return Promise.resolve(body === undefined
    ? json(404, { code: 'not_found', detail: `the stub has no route for ${path}` })
    : json(200, body));
}

let Home: typeof import('@/app/page').default;
let client: typeof import('@/lib/api');

beforeEach(async () => {
  vi.useFakeTimers();
  vi.stubGlobal('scrollTo', vi.fn());
  vi.resetModules();
  // `apiMode` is read once, when `lib/api.ts` is evaluated — so the stub has to be in place before
  // the import, and the import has to be fresh. Hence `resetModules` above and dynamic imports here:
  // a top-level `import Home` would have bound the sample adapter and this file would be testing the
  // wrong client while claiming to test the live one.
  vi.stubEnv('NEXT_PUBLIC_API_MODE', 'live');
  vi.stubGlobal('fetch', vi.fn(backend));
  client = await import('@/lib/api');
  Home = (await import('@/app/page')).default;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.resetModules();
});

async function openWorkspace() {
  // The token is in the client before the first render, which is the state the owner is in after
  // pasting it: held in memory for this tab, never in storage.
  client.setOwnerToken(TOKEN);
  await act(async () => { render(<Home />); });
  await settle();
}

async function openSettings() {
  const settings = screen.getAllByRole('button', { name: 'Settings' })[0]!;
  await act(async () => { fireEvent.click(settings); });
  await settle(2);
}

test('the workspace reads the owner’s own data once the token is held', async () => {
  await openWorkspace();
  // The positive half, so the assertions after the click are about a screen that HAD something to
  // lose. Without it, a build that never loaded anything would pass the test below.
  expect(screen.getByLabelText(`Signed in as ${OWNER_NAME}`)).toBeInTheDocument();
  expect(screen.getByText('Alex asked to move an appointment.')).toBeInTheDocument();
  expect(screen.getByText('Ask before acting')).toBeInTheDocument();
  await openSettings();
  expect(screen.getByText(new RegExp(OWNER_ADDRESS))).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Pause meeting changes' })).toBeInTheDocument();
  expect((screen.getByLabelText('Protected start') as HTMLInputElement).value).toBe(LOADED_BOUNDARY);
});

test('forgetting the token takes the private state off the screen with it', async () => {
  await openWorkspace();
  await openSettings();
  expect(screen.getByText(new RegExp(OWNER_ADDRESS))).toBeInTheDocument();

  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Forget token' })); });
  await settle();

  // What the owner is told, and what they can still see, have to agree. Both halves are asserted:
  // a screen that cleared the data but still said a token was held would fail here too.
  expect(screen.getByText(/No token is held/)).toBeInTheDocument();
  expect(client.ownerToken()).toBeNull();
  // The credential is gone from the client, so everything read with it goes with it: the owner's
  // name in the app bar, the connection the server reported WITH the owner's own address in it, the
  // routine the session said was running, and the availability the session reported.
  expect(screen.queryByLabelText(`Signed in as ${OWNER_NAME}`)).toBeNull();
  expect(screen.queryByText(new RegExp(OWNER_ADDRESS))).toBeNull();
  expect(screen.queryByRole('button', { name: 'Pause meeting changes' })).toBeNull();
  expect((screen.getByLabelText('Protected start') as HTMLInputElement).value)
    .not.toBe(LOADED_BOUNDARY);
});

test('forgetting the token empties the field a replacement was typed into', async () => {
  // The seventh review's Medium. `Forget token` cleared the token the client was USING and left
  // whatever was in the password input sitting there — so an owner who pastes a replacement and
  // then clicks Forget keeps a live credential on screen under a status line reading "No token is
  // held". Same two-claims-one-screen defect as the stale day, one field over, and the assertion is
  // about the field's own value rather than about any label.
  await openWorkspace();
  await openSettings();

  const field = screen.getByLabelText('Owner token') as HTMLInputElement;
  await act(async () => { fireEvent.change(field, { target: { value: 'a-replacement-token-typed-but-not-used' } }); });
  expect(field.value).toBe('a-replacement-token-typed-but-not-used');

  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Forget token' })); });
  await settle();

  expect(screen.getByText(/No token is held/)).toBeInTheDocument();
  expect((screen.getByLabelText('Owner token') as HTMLInputElement).value).toBe('');
});

test('a read still in flight cannot put the private state back after the token is gone', async () => {
  // The race, driven rather than assumed. The read held back here is `GET /v2/limits`, and it is the
  // one that matters: it is owner-gated, it is started by an effect the moment the session lands, and
  // its answer is the owner's standing autonomy — which the app bar renders on EVERY screen,
  // including the Settings screen the Forget button lives on. So this is not a hypothetical: forget
  // the token while this read is out and, without a guard, the chip says "Ask me every time" one line
  // above "No token is held".
  //
  // The day's own reads are guarded by `generation` and are covered by the test above; this one is
  // the effect that had no guard at all.
  let release: (() => void) | null = null;
  const held = new Promise<void>(resolve => { release = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (new URL(url, window.location.origin).pathname === '/v2/limits') await held;
    return backend(url, init);
  }));

  client.setOwnerToken(TOKEN);
  await act(async () => { render(<Home />); });
  await settle();
  // The workspace is up on everything but the limits, so the screen has a real autonomy answer to
  // lose and the navigation the next line needs is enabled.
  expect(screen.getByLabelText(`Signed in as ${OWNER_NAME}`)).toBeInTheDocument();

  await openSettings();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Forget token' })); });
  await settle(2);
  expect(screen.queryByText('Ask me every time')).toBeNull();

  // Now the answer to a question asked with a credential the owner has since withdrawn.
  await act(async () => { release?.(); });
  await settle();
  expect(screen.queryByText('Ask me every time')).toBeNull();
  expect(screen.queryByText('Act within limits')).toBeNull();
  expect(screen.getByText(/No token is held/)).toBeInTheDocument();
});

test('a care-circle read from before a Forget cannot overwrite what loaded after it', async () => {
  // A review finding, Medium: `loadCare` (`GET /v2/care-circle`, `GET /v2/protected-facts`) had no
  // `generation` guard at all — unlike `load` and the limits poller, which this file's other tests
  // already cover. This app disables ALL navigation the instant `today` is cleared (`disabled={
  // !today || busy}` on the nav rail), which is exactly what `Forget token` does — so the one way
  // back to Care after a forget is to re-authenticate, which is also what bumps `generation` past
  // whatever a request still in flight from BEFORE the forget was made under. That stale request —
  // sent under the old session, resolving deep into a new one — must not be allowed to overwrite
  // what the new session actually loaded, which is the exact case this test drives: the circle
  // changes between the stale request being sent and it finally resolving, so a dropped guard and
  // a working one produce two different, checkable screens.
  const STALE_MEMBER = CARE_MEMBER_NAME;
  const CURRENT_MEMBER = 'Nova Circle';
  let careCircleCalls = 0;
  let releaseStale: (() => void) | null = null;
  const staleHeld = new Promise<void>(resolve => { releaseStale = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const requestPath = new URL(url, window.location.origin).pathname;
    if (requestPath === '/v2/care-circle') {
      careCircleCalls += 1;
      const call = careCircleCalls;
      const headers = new Headers(init?.headers);
      if (headers.get('Authorization') !== `Bearer ${TOKEN}`)
        return json(403, { code: 'owner_forbidden', detail: 'This route belongs to the owner.' });
      if (call === 1) await staleHeld; // the request made before the forget below
      const name = call === 1 ? STALE_MEMBER : CURRENT_MEMBER;
      return json(200, { members: [{ id: `cm-${call}`, name, email: 'cm@example.test',
        may_receive: ['digest'], added_by: 'owner', added_at: '2026-09-19T09:00:00+01:00' }] });
    }
    return backend(url, init);
  }));

  await openWorkspace(); // mount fires call #1 to `/v2/care-circle` — held
  const care = screen.getAllByRole('button', { name: 'Care' })[0]!;

  await openSettings();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Forget token' })); });
  await settle(2);
  expect(screen.getByText(/No token is held/)).toBeInTheDocument();
  expect(care).toHaveAttribute('disabled'); // `!today` — re-authenticating is the only way back

  // Re-authenticate. This is what re-enables navigation, and — same as `load` — bumps `generation`
  // past the still-pending call #1's own.
  const field = screen.getByLabelText('Owner token') as HTMLInputElement;
  await act(async () => { fireEvent.change(field, { target: { value: TOKEN } }); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Use this token' })); });
  await settle(4);
  expect(screen.getByLabelText(`Signed in as ${OWNER_NAME}`)).toBeInTheDocument();

  await act(async () => { fireEvent.click(care); });
  await settle(2); // call #2, unheld, current data
  expect(screen.getByText(CURRENT_MEMBER)).toBeInTheDocument();
  expect(screen.queryByText(STALE_MEMBER)).toBeNull();

  // Call #1 — sent before the forget, in a generation nothing on screen belongs to any more —
  // finally resolves.
  await act(async () => { releaseStale?.(); });
  await settle();
  expect(screen.getByText(CURRENT_MEMBER)).toBeInTheDocument();
  expect(screen.queryByText(STALE_MEMBER)).toBeNull();
});
