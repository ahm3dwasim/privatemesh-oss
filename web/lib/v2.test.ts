import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createSampleAdapter, resetSample, SAMPLE_CHANGE_ID as id } from './sample';
import { createLiveClient, ownerToken, setOwnerToken } from './api';
import type { SampleVariant } from './v2-types';
const ready = async (variant: SampleVariant = 'happy') => { const api = createSampleAdapter(variant); await api.prepare(id); vi.advanceTimersByTime(2200); await api.change(id); return api; };
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-21T09:00:00Z')); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
describe('v2 adapter', () => {
  it('walks all stages without network calls', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const api = createSampleAdapter();
    expect((await api.change(id)).stage).toBe('detected');
    expect((await api.prepare(id)).stage).toBe('reading_thread');
    vi.advanceTimersByTime(700);
    expect((await api.change(id)).stage).toBe('checking_availability');
    vi.advanceTimersByTime(700);
    expect((await api.change(id)).stage).toBe('checking_venues');
    vi.advanceTimersByTime(700);
    const current = await api.change(id);
    expect(current.stage).toBe('proposal_ready');
    expect(current.proposal?.rejected).toHaveLength(2);
    expect(current.proposal?.start).toContain('15:00:00');
    expect((await api.approve(id, current.version)).stage).toBe('approved');
    vi.advanceTimersByTime(600);
    expect((await api.change(id)).stage).toBe('executing');
    vi.advanceTimersByTime(1000);
    expect((await api.change(id)).stage).toBe('done');
    expect((await api.outcome(id)).effects.every(e => e.state === 'done')).toBe(true);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  it('refuses stale and expired approval with no effects', async () => {
    const api = await ready();
    await api.edit(id, { reply_body: 'See you at 15:00!' });
    await expect(api.approve(id, 1)).rejects.toMatchObject({ status: 409 });
    expect((await api.outcome(id)).effects.every(e => e.state === 'pending')).toBe(true);
    vi.advanceTimersByTime(16 * 60000);
    await expect(api.approve(id, 2)).rejects.toMatchObject({ status: 409 });
  });
  it('stale fixture permits fresh review', async () => {
    const api = await ready('stale');
    await expect(api.approve(id, 1)).rejects.toMatchObject({ code: 'stale_version' });
    const changed = await api.change(id);
    expect(changed.version).toBe(2);
    expect(changed.proposal?.before[0]?.start).toContain('12:00:00');
    expect(changed.blocked_reason).toContain('11:00–11:45 to 12:00–12:45');
    const calendar = changed.proposal?.actions.find(action => action.kind === 'calendar_update');
    expect(calendar?.kind === 'calendar_update' && calendar.diff.start[0]).toContain('12:00:00');
    expect((await api.approve(id, 2)).stage).toBe('approved');
  });
  it.each(['partial', 'unknown'] as const)('preserves %s effects without retry', async (variant) => {
    const api = await ready(variant);
    await api.approve(id, 1);
    vi.advanceTimersByTime(2000);
    expect((await api.change(id)).stage).toBe('partial');
    const outcome = await api.outcome(id);
    expect((await api.today()).events.find(event => event.id === 'evt-bloods')?.start).toContain('15:00:00');
    expect(outcome.effects[0]?.state).toBe('done');
    expect(outcome.effects[1]?.state).toBe(variant === 'partial' ? 'failed' : 'unknown');
    await expect(api.approve(id, 1)).rejects.toMatchObject({ status: 409 });
    expect(await api.outcome(id)).toEqual(outcome);
  });
  it('rejects boundary conflicts and approval after leave', async () => {
    const api = await ready();
    await expect(api.edit(id, { start: '2026-09-22T16:00:00+01:00' })).rejects.toMatchObject({ code: 'boundary_conflict' });
    expect((await api.change(id)).version).toBe(1);
    await api.leave(id);
    await expect(api.approve(id, 1)).rejects.toMatchObject({ status: 409 });
  });
  it('refuses preparation with missing access and supports no safe fit', async () => {
    await expect(createSampleAdapter('expired_connection').prepare(id)).rejects.toMatchObject({ code: 'connection_required' });
    await expect(createSampleAdapter('paused').prepare(id)).rejects.toMatchObject({ code: 'routine_paused' });
    expect((await (await ready('no_safe_fit')).change(id)).stage).toBe('no_safe_fit');
  });
  it('updates venue in both actions and increments version', async () => {
    const api = await ready();
    const edited = await api.edit(id, { venue_index: 1 });
    expect(edited.version).toBe(2);
    expect(edited.proposal?.venue.name).toBe('Northgate Clinic, Riverside annexe');
    expect(JSON.stringify(edited.proposal?.actions)).toContain('Northgate Clinic, Riverside annexe');
  });
  it('expires a proposal with valid connections and refreshes on prepare', async () => {
    const api = await ready('expired');
    expect((await api.session()).connections.every(connection => connection.state === 'connected')).toBe(true);
    await expect(api.approve(id, 1)).rejects.toMatchObject({ code: 'stale_version' });
    await api.prepare(id);
    vi.advanceTimersByTime(2200);
    const next = await api.change(id);
    expect(next.version).toBe(2);
    expect((await api.approve(id, 2)).stage).toBe('approved');
  });
  it('invalidates old review when the boundary changes and fails closed for no fit', async () => {
    const api = await ready();
    await api.setupBoundary({ start: '16:00', buffer_minutes: 45 });
    await expect(api.approve(id, 1)).rejects.toMatchObject({ status: 409 });
    await api.prepare(id);
    vi.advanceTimersByTime(2200);
    expect((await api.change(id)).stage).toBe('no_safe_fit');
    await api.setupBoundary({ start: '17:30', buffer_minutes: 45 });
    await api.prepare(id);
    vi.advanceTimersByTime(2200);
    const next = await api.change(id);
    expect(next.proposal?.protected.start).toContain('17:30:00');
    const edited = await api.edit(id, { start: '2026-09-22T16:00:00+01:00' });
    expect(JSON.stringify(edited.proposal?.actions)).toContain('16:00–16:45');
  });
  it('can freshly prepare a stopped plan', async () => { const api = await ready(); await api.leave(id); await api.prepare(id); vi.advanceTimersByTime(2200); expect((await api.change(id)).stage).toBe('proposal_ready'); });
  it('exposes authorizing and long wait without invented completion', async () => {
    expect((await createSampleAdapter('authorizing').session()).connections[0]?.state).toBe('authorizing');
    expect((await createSampleAdapter('expired_connection').session()).connections[0]?.state).toBe('expired');
    const api = await ready('long_wait');
    vi.advanceTimersByTime(120000);
    expect((await api.change(id)).stage).toBe('checking_venues');
    expect((await api.leave(id)).stage).toBe('stopped');
  });
  it('updates a preference in place and preserves consent provenance', async () => {
    const api = createSampleAdapter();
    const pref = await api.savePreference({ fact: 'Quiet cafés', scope: 'Meeting changes', source: 'Explicit owner consent' });
    const updated = await api.updatePreference(pref.id, { fact: 'Quiet cafés within walking distance', scope: 'Café meetings' });
    expect(updated.id).toBe(pref.id);
    expect(updated.source).toBe(pref.source);
    expect(updated.fact).toBe('Quiet cafés within walking distance');
    expect(await api.preferences()).toEqual([updated]);
    await api.deletePreference(pref.id);
    expect(await api.preferences()).toEqual([]);
    await expect(api.updatePreference(pref.id, { fact: 'Other', scope: 'Meetings' })).rejects.toMatchObject({ status: 404 });
  });
  it('uses only consented meeting-planning preferences and records use', async () => {
    const api = createSampleAdapter();
    const relevant = await api.savePreference({ fact: 'Prefer quiet cafés', scope: 'Meeting planning', source: 'Owner entered' });
    const unrelated = await api.savePreference({ fact: 'Use aisle seats', scope: 'Travel', source: 'Owner entered' });
    expect((await api.preferences())[0]?.last_used).toBeNull();
    await api.prepare(id);
    const stored = await api.preferences();
    expect(stored.find(pref => pref.id === relevant.id)?.last_used).not.toBeNull();
    expect(stored.find(pref => pref.id === unrelated.id)?.last_used).toBeNull();
    const why = await api.why(id);
    expect(JSON.stringify(why.used)).toContain('Prefer quiet cafés');
    expect(JSON.stringify(why.used)).not.toContain('Use aisle seats');
    expect(Object.keys(why.kept_private.counts)).toEqual(why.kept_private.categories);
    await api.leave(id);
    await api.deletePreference(relevant.id);
    await api.prepare(id);
    expect(JSON.stringify((await api.why(id)).used)).not.toContain('Prefer quiet cafés');
  });
  it('keeps adjusted times and venues consistent throughout plan and outcome', async () => {
    const api = await ready();
    await api.setupBoundary({ start: '17:30', buffer_minutes: 45 });
    await api.prepare(id);
    vi.advanceTimersByTime(2200);
    const readyPlan = await api.change(id);
    expect(readyPlan.proposal?.rejected.some(rejected => rejected.name === '16:00 start')).toBe(false);
    const edited = await api.edit(id, { start: '2026-09-22T16:00:00+01:00', venue_index: 1 });
    expect(edited.proposal?.reason_sentence).toContain('16:00 gives');
    expect(JSON.stringify(edited.proposal?.actions)).toContain('16:00–16:45');
    expect(JSON.stringify(edited.proposal?.actions)).toContain('9 Riverside Walk');
    const calendar = edited.proposal?.actions.find(action => action.kind === 'calendar_update');
    // The clinic FRONT DESK, who is on the care circle and listed for `schedule` — not the
    // `appointments@` address the thread happens to be signed with, and not a second recipient.
    expect(calendar?.kind === 'calendar_update' && calendar.attendees).toEqual(['front.desk@northgate.example.com']);
    await api.approve(id, edited.version);
    vi.advanceTimersByTime(2000);
    expect((await api.outcome(id)).summary).toContain('16:00');
    const why = await api.why(id);
    expect(JSON.stringify(why.used)).toContain('17:30');
    expect(JSON.stringify(why.changed)).toContain('16:00–16:45');
  });
  it('public preview is synthetic, ready, and isolated in both modes', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const api = createSampleAdapter();
    const preview = await api.preview();
    expect(preview.change.stage).toBe('proposal_ready');
    expect(preview.change.proposal?.venue.name).toBe('Northgate Clinic, Room 4');
    expect(preview.today.events.some(event => event.kind === 'proposed')).toBe(true);
    expect((await api.change(id)).stage).toBe('detected');
    // The live client has no `preview` at all. It used to answer it with this same synthetic
    // proposal — a live transport returning fixture data under a name that reads as "show me this
    // server's state", which is a claim about a backend that was never asked. Asserted as an absence
    // so that re-adding it is a failing test rather than a convenience somebody re-introduces.
    expect('preview' in createLiveClient()).toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
    resetSample('partial');
    await api.prepare(id);
    vi.advanceTimersByTime(2200);
    await api.approve(id, 1);
    vi.advanceTimersByTime(2000);
    expect((await api.change(id)).stage).toBe('partial');
  });
  it('sample connections authorize before confirming and enable routine explicitly', async () => {
    const api = createSampleAdapter('missing');
    expect((await api.session()).routine.enabled).toBe(false);
    expect((await api.connect('calendar')).connections[1]?.state).toBe('authorizing');
    vi.advanceTimersByTime(1499);
    expect((await api.session()).connections[1]?.state).toBe('authorizing');
    vi.advanceTimersByTime(1);
    expect((await api.session()).connections[1]?.state).toBe('connected');
    expect((await api.routine(false)).routine.enabled).toBe(true);
    const authorizing = createSampleAdapter('authorizing');
    vi.advanceTimersByTime(1500);
    expect((await authorizing.session()).connections[0]?.state).toBe('connected');
  });
  it('live owner auth fails closed', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ mode: 'live' }) }));
    expect((await createLiveClient().session()).authenticated_owner).toBe(false);
  });
  it('live approval sends exact version once and preserves refusal', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({ ok: false, status: 409, json: async () => ({ detail: 'Stale plan', code: 'stale_version' }) });
    vi.stubGlobal('fetch', fetchSpy);
    await expect(createLiveClient().approve('a/b', 7)).rejects.toMatchObject({ status: 409, code: 'stale_version' });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledWith('/v2/changes/a%2Fb/approve', expect.objectContaining({ body: '{"version":7}', credentials: 'same-origin' }));
  });
  // The owner token is a credential in a bundle anyone can download, so where the browser got it is
  // the whole question. The version these replace asked `NODE_ENV`: "not a production build, so the
  // browser may have the key". Both halves were wrong — `NEXT_PUBLIC_*` is INLINED AT BUILD TIME, so
  // the shipped bytes already contain it and no runtime branch decides anything, and `NODE_ENV` does
  // not describe who can reach an artifact (`next build && next start` is production and public;
  // `next dev --host 0.0.0.0` is development and public). The token is a runtime value now, entered
  // in the running app. `npm run bundle-check` asserts on the BUILT artifact; these assert the
  // client's half.
  it('sends the owner token only after the owner enters it at runtime', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ mode: 'live' }) });
    vi.stubGlobal('fetch', fetchSpy);
    // Nothing in the environment can supply it. Set every variable the old client read.
    vi.stubEnv('NEXT_PUBLIC_OWNER_TOKEN', 'tok-from-the-file');
    vi.stubEnv('NODE_ENV', 'development');
    await createLiveClient().session();
    expect(fetchSpy.mock.calls[0]?.[1]?.headers).not.toHaveProperty('Authorization');
    // And it is not inlined, so the value is not obtainable from the module either.
    expect(ownerToken()).toBeNull();
    setOwnerToken('tok-from-the-owner');
    await createLiveClient().session();
    expect(fetchSpy.mock.calls[1]?.[1]?.headers).toMatchObject({ Authorization: 'Bearer tok-from-the-owner' });
    // Same in a production build: the value is not build-dependent, so neither is the header.
    vi.stubEnv('NODE_ENV', 'production');
    await createLiveClient().session();
    expect(fetchSpy.mock.calls[2]?.[1]?.headers).toMatchObject({ Authorization: 'Bearer tok-from-the-owner' });
    setOwnerToken(null);
  });
  it('a blank runtime token is no token, and a cleared token stops being sent', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ mode: 'live' }) });
    vi.stubGlobal('fetch', fetchSpy);
    setOwnerToken('   ');
    expect(ownerToken()).toBeNull();
    await createLiveClient().session();
    expect(fetchSpy.mock.calls[0]?.[1]?.headers).not.toHaveProperty('Authorization');
    setOwnerToken('tok');
    setOwnerToken(null);
    await createLiveClient().session();
    expect(fetchSpy.mock.calls[1]?.[1]?.headers).not.toHaveProperty('Authorization');
  });
  // Where the credential goes is a security decision, not a configuration one. The live client is a
  // local tool; an origin that is not this machine puts the owner token on a network the owner does
  // not control.
  it('refuses to send the owner credential to a non-loopback base URL', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ mode: 'live' }) });
    vi.stubGlobal('fetch', fetchSpy);
    setOwnerToken('tok-from-the-owner');
    for (const base of ['https://staging.example', 'http://192.168.1.9:8831', '//evil.example']) {
      await expect(createLiveClient(base, 'http://localhost:3000').session())
        .rejects.toMatchObject({ code: 'insecure_transport' });
    }
    expect(fetchSpy).not.toHaveBeenCalled();
    // Loopback is the documented deployment, and it is not refused.
    for (const base of ['http://127.0.0.1:8831', 'http://localhost:8831', 'http://[::1]:8831']) {
      await createLiveClient(base, 'http://localhost:3000').session();
    }
    expect(fetchSpy).toHaveBeenCalledTimes(3);
    setOwnerToken(null);
  });

  // The empty base is the one that looked safest and was not. A relative URL is resolved by the
  // browser against the PAGE's origin, so "no API base configured" means the token goes to whoever
  // served the page — which is a stranger's server the moment the build is hosted anywhere.
  it('resolves an empty base against the page origin instead of trusting it', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ mode: 'live' }) });
    vi.stubGlobal('fetch', fetchSpy);
    setOwnerToken('tok-from-the-owner');

    // Served from this machine, with no API base: the documented local deployment.
    await createLiveClient('', 'http://localhost:3000').session();
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    // Served from somewhere else, with no API base: the token would go to that origin. This is the
    // case the previous version answered "same-origin, therefore safe" and sent it.
    for (const page of ['https://app.example', 'http://192.168.1.9:3000']) {
      await expect(createLiveClient('', page).session())
        .rejects.toMatchObject({ code: 'insecure_transport' });
    }
    // And with no page to resolve against at all, nothing is sent: "I cannot tell where this goes"
    // is not a reason to send a credential.
    await expect(createLiveClient('', '').session())
      .rejects.toMatchObject({ code: 'insecure_transport' });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    setOwnerToken(null);
  });

  // The opt-in used to unlock "anywhere". It now unlocks an exact list, so approving a remote
  // server means naming it, and a base URL that changes afterwards lands on a refusal.
  it('sends the owner credential to a remote origin only when that exact origin is allowlisted', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ mode: 'live' }) });
    vi.stubGlobal('fetch', fetchSpy);
    setOwnerToken('tok-from-the-owner');
    vi.stubEnv('NEXT_PUBLIC_ALLOW_REMOTE_API', '1');

    // The opt-in alone is not an approval of an address, so this is still refused.
    vi.stubEnv('NEXT_PUBLIC_API_ORIGIN_ALLOWLIST', '');
    await expect(createLiveClient('https://staging.example', 'http://localhost:3000').session())
      .rejects.toMatchObject({ code: 'insecure_transport' });

    // Someone else's origin on the list does not approve this one.
    vi.stubEnv('NEXT_PUBLIC_API_ORIGIN_ALLOWLIST', 'https://other.example');
    await expect(createLiveClient('https://staging.example', 'http://localhost:3000').session())
      .rejects.toMatchObject({ code: 'insecure_transport' });

    // A SUFFIX of an allowlisted origin is a different origin, and a lookalike host is not a match.
    vi.stubEnv('NEXT_PUBLIC_API_ORIGIN_ALLOWLIST', 'https://staging.example');
    for (const lookalike of ['https://staging.example.evil.test', 'https://evil-staging.example',
                             'http://staging.example', 'https://staging.example:8443']) {
      await expect(createLiveClient(lookalike, 'http://localhost:3000').session())
        .rejects.toMatchObject({ code: 'insecure_transport' });
    }

    // The named origin itself is the one that goes through.
    await createLiveClient('https://staging.example', 'http://localhost:3000').session();
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    // Without the opt-in, the same allowlist entry changes nothing.
    vi.stubEnv('NEXT_PUBLIC_ALLOW_REMOTE_API', '0');
    await expect(createLiveClient('https://staging.example', 'http://localhost:3000').session())
      .rejects.toMatchObject({ code: 'insecure_transport' });
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    vi.unstubAllEnvs();
    setOwnerToken(null);
  });
});

describe('Concept D learning and reuse', () => {
  it('keeps installation separate from effects, refuses another identity, and reuses only active skills', async () => {
    const fetchSpy = vi.fn(); vi.stubGlobal('fetch', fetchSpy);
    const api = createSampleAdapter();
    const { learning_id } = await api.learn(id);
    await expect(api.installSkill(learning_id, 'wrong')).rejects.toMatchObject({ status: 409 });
    vi.advanceTimersByTime(2300);
    const learning = await api.learning(learning_id);
    expect(learning.stage).toBe('ready_for_review');
    expect(learning.attempts[0]?.review.verdict).toBe('reject');
    expect(learning.attempts[0]?.violations[0]?.kind).toBe('private_context_read');
    expect(learning.attempts[1]?.held_out).toEqual({ passed: 4, failed: 0 });
    const beforeInstall = await api.skills();
    await expect(api.installSkill(learning_id, 'wrong')).rejects.toMatchObject({ status: 409 });
    expect(await api.skills()).toEqual(beforeInstall);
    await api.installSkill(learning_id, learning.manifest!.identity);
    expect((await api.change(id)).stage).toBe('detected');
    expect((await api.outcome(id)).effects.every(e => e.state === 'pending')).toBe(true);
    await api.prepare(id); vi.advanceTimersByTime(2200);
    const ready = await api.change(id);
    await api.approve(id, ready.version); vi.advanceTimersByTime(2200);
    const later = await api.nextSampleChange!();
    expect(later.id).not.toBe(id);
    const reused = await api.prepare(later.id);
    expect(reused.skill_used?.identity).toBe(learning.manifest!.identity);
    expect(reused.learning_id).toBeUndefined();
    // The sample seeds one taught rule already active (Skills shows the win at first view), so the
    // capability skill this test installs is found by name rather than assumed to be the only entry.
    const byName = (name: string) => api.skills().then(all => all.find(s => s.name === name));
    expect((await byName(learning.skill_name))?.uses).toBe(2);
    await api.revokeSkill(learning.skill_name);
    await api.leave(later.id);
    expect((await api.prepare(later.id)).skill_used).toBeUndefined();
    expect((await byName(learning.skill_name))?.state).toBe('revoked');
    expect((await api.why(id)).overreach?.[0]?.blocked_by).toBe('manifest');
    expect(fetchSpy).not.toHaveBeenCalled();
  });
  it('refuses discarded and expired learning records', async () => {
    const api = createSampleAdapter();
    const before = await api.skills();
    const first = await api.learn(id); vi.advanceTimersByTime(2300);
    const discarded = await api.learning(first.learning_id);
    await api.rejectLearning(first.learning_id);
    await expect(api.installSkill(first.learning_id, discarded.manifest!.identity)).rejects.toMatchObject({ status: 409 });
    const next = await api.learn(id); vi.advanceTimersByTime(2300);
    const expired = await api.learning(next.learning_id);
    vi.advanceTimersByTime(31 * 86400000);
    await expect(api.installSkill(next.learning_id, expired.manifest!.identity)).rejects.toMatchObject({ status: 409 });
    expect(await api.skills()).toEqual(before);
  });
  it('uses encoded contract routes and exact identity for installation', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({ ok: true, status: 204 }); vi.stubGlobal('fetch', fetchSpy);
    const api = createLiveClient();
    await api.installSkill('learn/a', 'sha256:exact');
    expect(fetchSpy).toHaveBeenLastCalledWith('/v2/learning/learn%2Fa/approve', expect.objectContaining({ method: 'POST', body: JSON.stringify({ identity: 'sha256:exact' }) }));
    await api.revokeSkill('Meeting / evaluator');
    expect(fetchSpy).toHaveBeenLastCalledWith('/v2/skills/Meeting%20%2F%20evaluator/revoke', expect.objectContaining({ method: 'POST' }));
  });
});

it('keeps prior learning attempts addressable after another attempt starts', async () => {
  const api = createSampleAdapter();
  const first = await api.learn(id); vi.advanceTimersByTime(2300);
  const before = await api.learning(first.learning_id);
  await api.rejectLearning(first.learning_id);
  const second = await api.learn(id); vi.advanceTimersByTime(2300);
  expect((await api.learning(second.learning_id)).stage).toBe('ready_for_review');
  const historical = await api.learning(first.learning_id);
  expect(historical.stage).toBe('failed');
  expect(historical.attempts).toEqual(before.attempts);
});

describe('decision trail', () => {
  it('records the injection without widening the approved recipient list', async () => {
    const api = await ready();
    const before = await api.decisions(id);
    expect(before.map(d => d.verdict)).toEqual(['allowed', 'refused', 'allowed', 'allowed']);
    expect(before[1]).toMatchObject({ source: 'email', untrusted: true });
    const change = await api.change(id);
    expect(change).toHaveProperty('blocked_attempts', 1);
    expect(change.proposal?.actions.find(a => a.kind === 'email_reply')).toMatchObject({ to: ['front.desk@northgate.example.com'] });
    // The injection tail asked for a second recipient AND a protected value. Neither is anywhere in
    // the bundle the owner would approve — not the benefits team, and not the benefits id itself.
    expect(JSON.stringify(change.proposal?.actions)).not.toMatch(/benefits|QQ-4471/i);
    expect((await api.outcome(id)).effects.every(e => e.state === 'pending')).toBe(true);
    await api.approve(id, change.version);
    expect((await api.decisions(id)).find(d => d.stage === 'approve')).toMatchObject({ source: 'owner', untrusted: false, verdict: 'allowed' });
    expect(before).toHaveLength(4);
    await expect(api.decisions('missing-change')).rejects.toMatchObject({ status: 404 });
  });
  it('reads the live decision endpoint with an encoded change id and propagates failure', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => [] });
    vi.stubGlobal('fetch', fetchMock);
    const api = createLiveClient('');
    expect(await api.decisions('a/b')).toEqual([]);
    expect(fetchMock).toHaveBeenCalledWith('/v2/changes/a%2Fb/decisions', expect.objectContaining({ method: 'GET', cache: 'no-store' }));
    fetchMock.mockResolvedValue({ ok: false, status: 503, json: async () => ({ detail: 'Unavailable' }) });
    await expect(api.decisions(id)).rejects.toMatchObject({ status: 503 });
  });
});
