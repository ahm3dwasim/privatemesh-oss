/**
 * Human-readable calendar times for the demo screen.
 *
 * The proposal's `start`/`end` and a calendar action's `diff.start`/`diff.end` arrive as
 * ISO-8601 instants — `2026-09-22T13:00:00+01:00` — which is the wire format, not a sentence a
 * visitor reads. `formatDemoRange` is the one place that turns one into
 * "Tue 22 Sep, 13:00 → 13:45": both ends share a day, so the day is said once and only the clock
 * changes, the same convention a calendar app's own agenda view uses.
 *
 * `Europe/London` is fixed rather than read from the session, because every instant this screen
 * ever renders — the caregiver week's fixture, the protected commitment, every red-team case — is
 * stamped in it (`sim.TZ`); a demo that guessed the visitor's own timezone would show a time that
 * does not match the offset already printed in the ISO string beside it.
 */

const TIMEZONE = 'Europe/London';

// `en-US` rather than `en-GB` for the WEEKDAY/MONTH text, even though the clock below is `en-GB`'s
// 24-hour one: `en-GB`'s short month form is locale data that has read "Sep" in some ICU builds
// and "Sept" in others (the difference this file's own vitest run hit while it was being
// written), and `en-US`'s three-letter forms do not have that second spelling. The parts are
// picked out and reordered by hand — "Tue 22 Sep", day before month — rather than trusted to the
// formatter's own part order, which is `en-US`'s own (month before day) and not the one this
// screen wants.
const DAY_PARTS = new Intl.DateTimeFormat('en-US', {
  timeZone: TIMEZONE, weekday: 'short', day: 'numeric', month: 'short',
});
const CLOCK_FORMAT = new Intl.DateTimeFormat('en-GB', {
  timeZone: TIMEZONE, hour: '2-digit', minute: '2-digit', hour12: false,
});

/** One instant, as "Tue 22 Sep, 13:00". `null`/`undefined`/an unparsable string all answer `''` —
 *  a blank cell reads as "nothing to show", which is true, rather than as a JavaScript `Invalid
 *  Date` printed to a visitor. */
export function formatDemoInstant(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const parts = Object.fromEntries(DAY_PARTS.formatToParts(date).map(p => [p.type, p.value]));
  return `${parts.weekday} ${parts.day} ${parts.month}, ${CLOCK_FORMAT.format(date)}`;
}

/** A start/end pair on the same day, as "Tue 22 Sep, 13:00 → 13:45". The day is said once;
 *  a pair that (unusually, for this fixture) crosses midnight falls back to the day on BOTH ends
 *  rather than silently dropping the second one, so a reader is never left to guess which day
 *  13:45 belongs to. */
export function formatDemoRange(startIso: string | null | undefined, endIso: string | null | undefined): string {
  const start = formatDemoInstant(startIso);
  if (!endIso) return start;
  const end = formatDemoInstant(endIso);
  if (!start || !end) return start || end;
  const [startDay, startClock] = start.split(', ');
  const [endDay, endClock] = end.split(', ');
  return startDay === endDay ? `${start} → ${endClock}` : `${start} → ${end}`;
}
