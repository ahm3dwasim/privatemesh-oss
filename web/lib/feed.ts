import type { Change, ChangeSummary, Decision, EffectKind, Outcome, Today } from './v2-types';

/**
 * The Today feed: one thread per meeting, newest first, split into what needs the owner and what
 * already happened.
 *
 * It is a PURE function of what the API already returned. Every line below can be traced to a
 * field: the stage, `executed_by`, `blocked_attempts`, the proposal's own times and venue, the
 * decision log's verdicts. Nothing here summarises a check the engine did not report, and nothing
 * here reads a `Decision`'s `subject`, `rule` or `detail` INTO the page — those can carry protected
 * text. A subject is COMPARED against a known public string to pick a public phrase, which is the
 * same thing `RefusalCard` does and the only safe use of that field.
 *
 * ONE ROW PER CHANGE. A refusal that happened while handling a change used to be its own row,
 * sitting apart from the change it belongs to — two rows for one thread. It is now a note carried
 * ON the change's own row (`FeedItem.blocked`), because a change that refused something on the way
 * is still one story, not two.
 */
export type FeedTone = 'done' | 'waiting' | 'working' | 'blocked' | 'quiet';
/** The three words a status chip is ever allowed to say. Every stage below resolves to exactly one
 *  of them; the nuance goes in `detail`, not in a fourth word. `working` is not a fourth status — it
 *  is what `needs_you` looks like while it is still moving, styled the same and gone within seconds. */
export type FeedStatus = 'needs_you' | 'done' | 'blocked';

export interface FeedItem {
  /** Stable across reloads so React keeps focus and animation state. */
  key: string;
  change_id: string;
  status: FeedStatus;
  /** True only while `status === 'needs_you'` and the change is actively running (approved or
   *  executing). Styled identically to `needs_you`, just with a working pulse instead of a chip. */
  working: boolean;
  /** One sentence, plain language. Names the meeting, not just "a change". */
  line: string;
  /** The status words shown beside it. */
  statusLabel: string;
  tone: FeedTone;
  /** ISO timestamp used for ordering and for the relative time shown on the row. */
  at: string;
  /** A second, optional line — the sub-reason `detail` explains, never required to read the first. */
  detail?: string;
  /** Something this thread stopped or went around, carried ON this row rather than as a row of its
   *  own. Never rendered without the change it happened inside.
   *
   *  `kind` is not decoration. `refused` is a boundary refusal: a request was stopped and nothing
   *  happened. `withheld` is not a refusal of anything — the change proceeded and did not get
   *  everything it asked for, which is the protected commitment being planned around rather than
   *  described. Drawing the second with the first's words told the owner their rules had blocked
   *  something on a run where nothing was blocked, and told them nothing was sent on a run that
   *  sent. The note stays one field because it sits in one place on one row; what it must never be
   *  is one meaning. */
  blocked?: { kind: 'refused' | 'withheld' | 'stopped'; line: string; detail: string };
}

/** How one effect actually ended, with the two states that used to be collapsed kept apart.
 *
 *  `unknown` is the one this exists for. The old partial line read the change's STAGE and said "the
 *  reply did not send", which is a claim about the world that nothing observed: the sample's own
 *  `unknown` variant has a reply whose delivery was never confirmed, and a live run has exactly that
 *  whenever a provider call times out after the request left. An email that was in fact delivered,
 *  reported to its owner as not sent, is the most expensive sentence this app can print, and it is
 *  the one a stage-shaped summary reaches for by default. */
export type EffectReading = 'confirmed' | 'not_done' | 'unknown' | 'unfinished' | 'in_flight';

/** `pending` and `running` are NOT one reading, which is the sixth review's Medium.
 *
 *  Folding them together meant a send whose request was already in flight was described with the
 *  same words as a send nothing had attempted: "has not been sent". For a `pending` effect that is
 *  simply true. For a `running` one it is a claim about the world that nothing observed — the
 *  provider may have taken the message — and it is the same mistake, in the same direction, as
 *  reporting `unknown` as "not sent".
 *
 *  There is no `default` arm on purpose: the switch is exhaustive over the state union, so adding a
 *  state later is a compile error here rather than a silent fall-through to whichever reading the
 *  default happened to name. */
export function readEffect(effect: { state: 'pending' | 'running' | 'done' | 'failed' | 'unknown' }): EffectReading {
  switch (effect.state) {
    case 'done': return 'confirmed';
    case 'failed': return 'not_done';
    case 'unknown': return 'unknown';
    case 'running': return 'in_flight';
    case 'pending': return 'unfinished';
  }
}

/** One effect, in the owner's words, from the effect's OWN state.
 *
 *  Written once and used by both surfaces that describe effects — the Today row and the receipt on
 *  the change itself. They were two descriptions of one fact before, which is how they came to
 *  disagree: the row said the reply had not gone out while the receipt two clicks away listed the
 *  same effect as unknown. `meeting` is what the row calls the event it is describing. */
export function effectSentence(
  effect: { kind: EffectKind; state: 'pending' | 'running' | 'done' | 'failed' | 'unknown' },
  meeting: { who: string; when: string; where: string; what?: string },
): string {
  const { who, when, where } = meeting;
  const what = meeting.what ?? `your catch-up with ${who}`;
  const reading = readEffect(effect);
  // The digest's audience is a COUNT here for the same reason a refusal's is in the decision log:
  // this sentence is rendered into the Today row and into the receipt, and both are things that
  // get read over a shoulder, screenshotted and pasted into a bug report. Who exactly was told is
  // on the change's own screen, where the owner went to look.
  if (effect.kind === 'care_digest') {
    switch (reading) {
      case 'confirmed': return `Today’s family digest went to your care circle.`;
      case 'not_done': return `Today’s family digest was not sent — the send failed.`;
      case 'unknown': return `Whether today’s family digest reached your care circle is unknown — the send was not confirmed.`;
      case 'in_flight': return `Today’s family digest is going out — it may already have arrived.`;
      case 'unfinished': return `Today’s family digest has not been sent.`;
    }
  }
  if (effect.kind === 'calendar_update') {
    switch (reading) {
      case 'confirmed': return `Moved ${what} to ${when} at ${where}.`;
      case 'not_done': return `${capitalise(what)} was not moved — the calendar update failed.`;
      case 'unknown': return `Whether ${what} moved to ${when} at ${where} is unknown — the calendar update was not confirmed.`;
      case 'in_flight': return `${capitalise(what)} is being moved to ${when} at ${where} — the calendar update may already have gone through.`;
      case 'unfinished': return `The calendar update for ${what} has not finished.`;
    }
  }
  switch (reading) {
    case 'confirmed': return `${who} was told.`;
    case 'not_done': return `${who} was not told — the reply did not send.`;
    case 'unknown': return `Whether ${who} was told is unknown — the reply's delivery was not confirmed.`;
    case 'in_flight': return `The reply to ${who} is being sent — it may already have reached them.`;
    case 'unfinished': return `The reply to ${who} has not been sent.`;
  }
}

const hhmm = (value: string, timezone: string) => Number.isFinite(Date.parse(value))
  ? new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: timezone }).format(new Date(value))
  : 'an unavailable time';

/** The public phrase for one REFUSED decision — a request that was stopped. See the note above on
 *  `subject`. It takes only refusals: `withheld` is a different event with its own phrase below, and
 *  the version of this function that answered for both is how a withheld commitment came to be
 *  reported to the owner under the words "nothing was sent by the refused request". */
export function refusalPhrase(decision: Decision): string {
  if (decision.subject === 'Share a private detail with an extra recipient')
    return 'Refused: the email tried to add a recipient and share a private detail.';
  if (decision.stage === 'approve' && decision.source === 'owner')
    return 'Handed back to you: one of your limit clauses did not pass.';
  if (decision.source === 'email')
    return 'Refused: an email tried to widen what the assistant may do.';
  if (decision.source === 'model')
    return 'Refused: the model asked for something outside its permission.';
  if (decision.source === 'skill')
    return 'Refused: a learned skill asked for something outside its permission.';
  return 'Refused: a request went beyond what you allow.';
}

/** The public phrase for a WITHHELD decision. It says what happened — part of the request went
 *  ahead without everything it asked for — and says in the same breath that nothing was refused,
 *  because the two sentences a reader would otherwise supply ("your rules blocked this" and "nothing
 *  was sent") are both false here. Nothing was blocked and the change proceeded; what did not happen
 *  is that the planner got to read a private reason. */
export function withheldPhrase(): string {
  return 'Part of this request went ahead without everything it asked for.';
}

/** Sentence case for a subject that is about to start a sentence. Only the first letter, and only
 *  when it is a lowercase one: "Rosa's physiotherapy" must not become "ROSA's". */
function capitalise(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

/**
 * What this change is ABOUT, in the words the calendar already uses.
 *
 * The version this replaces said "your catch-up with {who}" on every row, which was true of the one
 * meeting the scheduling sample had and false of every appointment in a week of somebody else's
 * care. The event's own title is the honest answer and it is already on the proposal
 * (`before[0].title`), so a row can name the physio session, the transport pickup or the pharmacy
 * collection without this file knowing that any of those exist. A change with no proposal has no
 * event to name yet, so it falls back to the request rather than inventing a subject.
 */
export function subjectOf(change: Change, _who: string): string {
  const title = change.proposal?.before[0]?.title?.trim();
  // `an appointment` and not `${who}'s appointment`: the sender of a care thread is usually the
  // clinic, so possessing the appointment to them is wrong in the common case and wrong in a way
  // that reads as a bug ("Northgate Clinic's appointment"). A generic noun is the honest answer
  // when nothing the API returned names the event, and it is replaced by the real title the moment
  // a proposal exists.
  return title ? title : 'an appointment';
}

/** Whether this change's outbound is the family digest rather than a calendar edit and a reply.
 *  One predicate, because three places ask it and a fourth reading of `actions` is a fourth chance
 *  to disagree about what a change is. */
export function isDigest(change: Change): boolean {
  return (change.proposal?.actions ?? []).some(action => action.kind === 'care_digest');
}

function changeLine(change: Change, summary: ChangeSummary | undefined, timezone: string, today: string, outcome: Outcome | undefined, refusals: number): { line: string; statusLabel: string; status: FeedStatus; tone: FeedTone; working: boolean; detail?: string } {
  const who = summary?.from_name ?? 'Someone';
  const proposal = change.proposal;
  const when = proposal ? hhmm(proposal.start, timezone) : null;
  const where = proposal?.venue.name;
  const what = subjectOf(change, who);
  const digest = isDigest(change);
  const sent = proposal?.actions.find(a => a.kind === 'care_digest') as { to: string[]; day: string } | undefined;
  const recipients = sent?.to.length ?? 0;
  // A digest is named by the DAY it summarises, not by the day it is read on. "Today's family
  // digest went out" under a "Yesterday" heading is a sentence about the wrong day, and a week of
  // digests is exactly where that lands.
  const digestName = sent && sent.day !== today ? `The family digest for ${new Intl.DateTimeFormat('en-GB', { weekday: 'long', timeZone: timezone }).format(new Date(`${sent.day}T12:00:00`))}` : 'Today\u2019s family digest';
  switch (change.stage) {
    case 'done':
      if (digest)
        return { line: `${digestName} went to ${recipients} ${recipients === 1 ? 'person' : 'people'} on your care circle.`, statusLabel: 'Done', status: 'done', tone: 'done', working: false, detail: change.executed_by === 'limit' ? 'Sent within your limits — clause 7 passed, so it did not need to ask.' : 'You approved those exact words.' };
      return change.executed_by === 'limit'
        ? { line: `Moved ${what} to ${when} at ${where}, and replied to ${who}.`, statusLabel: 'Done', status: 'done', tone: 'done', working: false, detail: 'Done within your limits — every clause passed, so it did not need to ask.' }
        : { line: `Moved ${what} to ${when} at ${where}, and replied to ${who}.`, statusLabel: 'Done', status: 'done', tone: 'done', working: false, detail: 'You approved it.' };
    case 'partial': {
      // `partial` is not one fact and cannot be summarised as one. It means the change did not
      // fully finish, and WHICH part did not finish is a different answer in each of the sample's
      // own two variants: one has the invite updated and the reply failed, the other has the invite
      // updated and the reply's delivery unknown. Reading the stage and saying "the reply did not go
      // out" is right for the first and a false statement about the world for the second.
      const meeting = { who, when: when ?? 'an unavailable time', where: where ?? 'an unavailable venue', what };
      const line = outcome
        ? outcome.effects.map(effect => effectSentence(effect, meeting)).join(' ')
        : `Part of ${what} finished and part of it did not. The recorded result of each effect has not been read, so this row claims nothing about which.`;
      const unfinished = outcome?.effects.filter(effect => readEffect(effect) !== 'confirmed').length ?? 0;
      return { line, statusLabel: 'Blocked', status: 'blocked', tone: 'blocked', working: false,
        detail: unfinished === 1
          ? 'One effect is not confirmed. Nothing was retried automatically.'
          : 'Nothing was retried automatically. Check the recorded result before starting a new action.' };
    }
    case 'approved':
    case 'executing':
      return { line: digest ? 'Sending today’s family digest.' : `Carrying out the approved plan for ${what}.`, statusLabel: 'Working', status: 'needs_you', tone: 'working', working: true };
    case 'proposal_ready':
      if (digest)
        return { line: `${digestName} is ready for you to read.`, statusLabel: 'Needs you', status: 'needs_you', tone: 'waiting', working: false, detail: `Approving sends those exact words to ${recipients} ${recipients === 1 ? 'person' : 'people'} on your care circle. Nothing goes out until you do.` };
      return change.blocked_reason
        ? { line: `A plan for ${what} is ready, but one of your limits handed it back.`, statusLabel: 'Needs you', status: 'needs_you', tone: 'waiting', working: false, detail: change.blocked_reason }
        : { line: `${who} asked to move ${what}. A ${when} plan at ${where} is ready for you.`, statusLabel: 'Needs you', status: 'needs_you', tone: 'waiting', working: false };
    case 'no_safe_fit':
      // A change that ended with nothing produced has two quite different reasons for it, and they
      // are not a difference of degree. The day saying no is a scheduling fact the owner can act on
      // by moving something. A REFUSAL is a security event: a request was stopped, and the owner's
      // next question is what was asked for and whether any of it got out — never "which flexible
      // part would you like to change". Reading the first sentence over the second is how a blocked
      // attack came to be reported as a calendar inconvenience.
      return refusals > 0
        ? { line: `Refused ${who}’s email. Nothing was disclosed and nothing was sent.`, statusLabel: 'Blocked', status: 'needs_you', tone: 'blocked', working: false, detail: change.blocked_reason ?? 'Open it to read exactly what was asked for and what stopped it.' }
        : { line: `No time inside your boundaries fits ${who}’s request.`, statusLabel: 'Needs you', status: 'needs_you', tone: 'waiting', working: false, detail: change.blocked_reason ?? 'Choose which flexible part to change.' };
    case 'stopped':
      return { line: `Left ${what} unchanged. Nothing was sent.`, statusLabel: 'Done', status: 'done', tone: 'quiet', working: false, detail: 'You chose to leave this one alone.' };
    case 'detected':
      return { line: `${who} asked to move ${what}.`, statusLabel: 'Needs you', status: 'needs_you', tone: 'waiting', working: false };
    default:
      return { line: `Working out a new time for ${what}.`, statusLabel: 'Working', status: 'needs_you', tone: 'working', working: true };
  }
}

/** A stable identity for the real-world event a change's row describes: which calendar event, moved
 *  to exactly which start time. Two change records — a settled one from history and a live one still
 *  advancing — can end up describing the same event; `change.id` alone cannot catch that, because a
 *  version bump or a re-approval mints a new one. A change with no proposal yet has nothing to
 *  collide on, so it keys on its own id. */
function eventIdentity(change: Change): string {
  const calendar = change.proposal?.actions.find(a => a.kind === 'calendar_update');
  return calendar?.kind === 'calendar_update' ? `${calendar.event_id}@${calendar.diff.start[1]}` : `change:${change.id}`;
}

export function buildFeed(today: Today, changes: Change[], decisions: Record<string, Decision[]> = {}, outcomes: Record<string, Outcome> = {}): FeedItem[] {
  const summaries = new Map(today.changes.map(c => [c.id, c]));
  const items: FeedItem[] = [];
  const byIdentity = new Map<string, number>();
  for (const change of changes) {
    const summary = summaries.get(change.id);
    const rows = decisions[change.id] ?? [];
    const refused = rows.filter(d => d.verdict === 'refused');
    const withheld = rows.filter(d => d.verdict === 'withheld');
    const { line, statusLabel, status, tone, working, detail } = changeLine(change, summary, today.timezone, today.date, outcomes[change.id], refused.length);
    const at = Number.isFinite(Date.parse(change.updated_at)) ? change.updated_at : summary?.received_at ?? new Date(0).toISOString();
    // Everything this thread refused rides ON its own row — the fix for a refusal that used to be a
    // second row for the same change, which read as a duplicate of the meeting above it.
    //
    // The two verdicts are counted and phrased SEPARATELY. A `withheld` decision is not a refusal:
    // the change proceeded and did not get everything it asked for. Folding it into `refused` gave
    // the row the words "nothing was sent by the refused request" over a run where the invite moved
    // and the reply went out, and it put a refusal's weight — "your rules blocked something" — on
    // the one outcome where the owner's rules did exactly what they were written to do. A `refused`
    // is preferred when both are present: a stopped request is the larger fact.
    const blocked = refused.length
      ? { kind: 'refused' as const, line: refusalPhrase(refused[0]!), detail: refused.length > 1 ? `And ${refused.length - 1} more request${refused.length - 1 === 1 ? '' : 's'} refused on this thread.` : 'Nothing was sent by the refused request.' }
      : withheld.length
        ? { kind: 'withheld' as const, line: withheldPhrase(), detail: withheld.length > 1 ? `And ${withheld.length - 1} more parts went ahead without what they asked for. Nothing was refused on this thread.` : 'Nothing was refused here. A private reason was not described to the model, and the change went ahead without it.' }
        : change.blocked_attempts > 0
          // The decision log could not be read, but the change itself reports how many requests were
          // stopped. Say the number and nothing about which: guessing the reason here would be the one
          // invention this feed cannot afford. `blocked_attempts` is refused PLUS withheld, so even the
          // phrase "went beyond what you allow" was an attribution the count does not carry — a
          // withheld commitment did not go beyond anything, it was planned around. The row says how
          // many and stops.
          ? { kind: 'stopped' as const, line: `Stopped ${change.blocked_attempts} request${change.blocked_attempts === 1 ? '' : 's'} on this thread.`, detail: 'Open the change to read which check stopped it — this change reports the count, not the reasons.' }
          : undefined;
    const item: FeedItem = { key: `change-${change.id}`, change_id: change.id, status, working, line, statusLabel, tone, at, ...(detail ? { detail } : {}), ...(blocked ? { blocked } : {}) };
    // Deduplicate by the event, not the change id: the newer record (by `at`) wins, since it is the
    // more current telling of the same story — never both, which used to read as the same move
    // appearing twice in Recent activity.
    const identity = eventIdentity(change);
    const priorIndex = byIdentity.get(identity);
    if (priorIndex !== undefined) {
      if (Date.parse(at) > Date.parse(items[priorIndex]!.at)) items[priorIndex] = item;
      continue;
    }
    byIdentity.set(identity, items.length);
    items.push(item);
  }
  return items.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
}

/** A day heading for the "Recent activity" list: `Today`, `Yesterday`, or the weekday name — the
 *  three forms a person actually reads a nearby date as. `reference` is `today.date` so the fixture
 *  and the live server agree on what "today" means without touching `Date.now()`. */
export function dayHeading(at: string, reference: string, timezone: string): string {
  if (!Number.isFinite(Date.parse(at))) return 'Earlier';
  const fmt = (d: Date) => new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
  const day = fmt(new Date(at));
  const base = new Date(`${reference}T12:00:00`);
  if (day === reference) return 'Today';
  if (day === fmt(new Date(base.getTime() - 86400000))) return 'Yesterday';
  return new Intl.DateTimeFormat('en-GB', { timeZone: timezone, weekday: 'long' }).format(new Date(at));
}

/** Groups the "Recent activity" half of the feed under day headings, latest first, each day's own
 *  rows kept newest first inside it — the structure `TodayFeed` renders directly. */
export function groupByDay(items: FeedItem[], reference: string, timezone: string): { heading: string; items: FeedItem[] }[] {
  const groups: { heading: string; items: FeedItem[] }[] = [];
  for (const item of items) {
    const heading = dayHeading(item.at, reference, timezone);
    const last = groups.at(-1);
    if (last?.heading === heading) last.items.push(item);
    else groups.push({ heading, items: [item] });
  }
  return groups;
}
