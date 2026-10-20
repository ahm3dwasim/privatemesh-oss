import { buildFeed } from '@/lib/feed';
import type { Change, ChangeSummary, Decision, Today } from '@/lib/v2-types';

/**
 * The feed's half of the fifth review's High.
 *
 * The engine's finding was that `any_fit` was decided BEFORE the learned skill narrowed the
 * candidates, so a change where every remaining option was refused by the owner's own boundary was
 * recorded as `discarded` — "not chosen" — and the owner was told nothing. The Python test proves the
 * verdict is now `refused`. This file proves the OTHER end of the same wire: that the word reaches
 * the row the owner reads, because a correct verdict the feed drops on the floor is the same
 * silence with better bookkeeping.
 *
 * Both fixtures below are SERVER-SHAPED: they are what `GET /v2/changes/{id}` and
 * `GET /v2/changes/{id}/decisions` answer with, field for field, rather than the in-browser sample's
 * objects. The distinction is the sixth review's test-quality item — a fixture built from the same
 * helper the client uses agrees with the client by construction.
 */

const AT = '2026-09-19T09:05:00+01:00';

function change(over: Partial<Change> = {}): Change {
  return {
    executed_by: null,
    id: 'ch-1',
    version: 3,
    // No plan survived, so there is no proposal — `null`, not a proposal with empty fields. The
    // route answers `null` here and the feed must not read it as one.
    stage: 'no_safe_fit',
    request_excerpt: 'Can we push to later in the afternoon?',
    proposal: null,
    blocked_reason: null,
    blocked_attempts: 1,
    updated_at: AT,
    ...over,
  };
}

function summary(over: Partial<ChangeSummary> = {}): ChangeSummary {
  return {
    id: 'ch-1',
    from_name: 'Alex',
    from_address: 'alex@example.test',
    received_at: AT,
    excerpt: 'Can we push to later in the afternoon?',
    thread_url: null,
    stage: 'no_safe_fit',
    ...over,
  };
}

function today(over: Partial<Today> = {}): Today {
  return { date: '2026-09-19', timezone: 'Europe/London', events: [], protected: [],
    changes: [summary()], ...over };
}

/** One check the engine made, as the decisions route reports it. `seq`, `at` and `rule` are the
 *  fields the route always fills; only the four this file reasons about are ever varied. */
function decision(over: Partial<Decision> = {}): Decision {
  return {
    seq: 4,
    at: AT,
    stage: 'validate',
    source: 'owner',
    untrusted: false,
    subject: 'Refused 16:00–16:45',
    verdict: 'refused',
    rule: 'owner_availability_boundary',
    detail: 'This slot starts after 12pm, which your rule does not allow.',
    ...over,
  };
}

test('a boundary refusal the engine now records reaches the row the owner reads', () => {
  const rows = buildFeed(today(), [change()], { 'ch-1': [decision()] });
  expect(rows).toHaveLength(1);
  const row = rows[0]!;
  // The row is carried on the change it happened inside, not as a second row for the same meeting.
  expect(row.blocked?.kind).toBe('refused');
  expect(row.blocked?.line).toBe('Refused: a request went beyond what you allow.');
  // And the detail is the refusal's, not the count's: "Stopped 1 request on this thread" is what the
  // owner was shown while the verdict was wrong, and it names no reason at all.
  expect(row.blocked?.detail).toBe('Nothing was sent by the refused request.');
});

test('a change that ended in a refusal does not read as a day with no free slot', () => {
  // `no_safe_fit` is the terminal stage for two quite different endings, and they are not a
  // difference of degree. The day saying no is a scheduling fact the owner fixes by moving
  // something; a refusal is a security event, and the next question is what got out. Reading the
  // first sentence over the second is how a blocked attack came to be reported as a calendar
  // inconvenience, which is exactly what the caregiver week's lookalike would have been.
  const refused = buildFeed(today(), [change()], { 'ch-1': [decision()] })[0]!;
  expect(refused.line).toBe('Refused Alex\u2019s email. Nothing was disclosed and nothing was sent.');
  expect(refused.tone).toBe('blocked');

  const noFit = buildFeed(today(), [change()], { 'ch-1': [] })[0]!;
  expect(noFit.line).toBe('No time inside your boundaries fits Alex\u2019s request.');
  expect(noFit.tone).toBe('waiting');
});

test('the count alone is never promoted into a refusal, and a discarded slot is not one', () => {
  // The same change, the same `blocked_attempts: 1` — and the only rows the engine wrote are
  // `discarded`, which is what the boundary check writes when a plan SURVIVED by choosing another
  // slot. Nothing was stopped here, so the feed may report the number and must not report a refusal.
  // This is the half that fails if the fix were "map a nonzero count to `refused`".
  const discarded = decision({ verdict: 'discarded', subject: 'Not chosen 16:00–16:45' });
  const rows = buildFeed(today(), [change({ blocked_attempts: 1 })], { 'ch-1': [discarded] });
  expect(rows[0]!.blocked?.kind).toBe('stopped');
  expect(rows[0]!.blocked?.line).toBe('Stopped 1 request on this thread.');
  expect(rows[0]!.blocked?.line).not.toMatch(/refus|beyond/i);
});

test('a withheld decision is phrased as neither a refusal nor a bare count', () => {
  // The third verdict, which the same code path has to keep distinct: part of the request went ahead
  // without everything it asked for. Present so the fix above cannot be read as collapsing the
  // vocabulary back into two words.
  const withheld = decision({ verdict: 'withheld', subject: 'Availability of a protected block' });
  const rows = buildFeed(today(), [change({ stage: 'proposal_ready', blocked_attempts: 1 })],
    { 'ch-1': [withheld] });
  expect(rows[0]!.blocked?.kind).toBe('withheld');
  expect(rows[0]!.blocked?.line).toBe('Part of this request went ahead without everything it asked for.');
});
