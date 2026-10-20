import type { Change, Today, Outcome } from '@/lib/v2-types';
export function time(value: string, timezone = 'Europe/London') {
  if (!Number.isFinite(Date.parse(value)))
    return 'Time unavailable'; return new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: timezone }).format(new Date(value));
}
export function Day({ today, change, outcome, compact = false }: {
  today: Today;
  change: Change | null;
  outcome?: Outcome | null;
  compact?: boolean;
}) {
  const proposal = change?.proposal;
  const boundary = proposal?.protected ?? today.protected[0];
  const eventId = proposal?.actions.find(a => a.kind === 'calendar_update')?.event_id;
  // The event this plan is about, or — with no plan on screen — the day's first event. The
  // fallback in the middle used to be `/alex/i.test(e.title)`, one fixture's name hard-coded into
  // the component that draws everybody's day; on a caregiver's week it matched nothing and the
  // panel fell through to `events[0]` by accident rather than by intent.
  const current = proposal?.before.find(e => e.id === eventId) ?? today.events[0];
  // Everything on the day before the protected boundary, IN ORDER. A caregiver's day has several
  // appointments in it, and a panel that drew the one under change first and the rest after it was
  // drawing a list in fetch order and calling it a day.
  const minutes = (event: { start: string; end: string }) => Math.round((Date.parse(event.end) - Date.parse(event.start)) / 60000);
  const ordered = today.events
    .filter(event => Date.parse(event.start) < Date.parse(boundary?.start ?? '') && event.kind !== 'proposed')
    .sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
  const confirmed = outcome?.effects.find(e => e.kind === 'calendar_update')?.state === 'done';
  const boundaryTime = boundary ? time(boundary.start, today.timezone) : 'Not set';
  const bufferStart = boundary ? time(new Date(Date.parse(boundary.start) - boundary.buffer_minutes * 60000).toISOString(), today.timezone) : '—';
  return <section className={`day ${compact ? 'compact' : ''}`} aria-label="Current and proposed day">
    {/* One stable title always — the state (whether there is a plan to place) belongs in the
        subtitle, never in the heading itself, which used to change between "A place for this plan."
        and "Your day, with a boundary." for no reason a reader could see. */}
    <div className="day-heading"><div><h2>Your day</h2><p>{proposal ? 'A place for this plan' : 'With a boundary'} · {today.date} · {today.timezone}</p></div><span className="day-icon" aria-hidden="true">▦</span></div>
    <div className="day-columns">{['Current', proposal ? confirmed ? 'Confirmed' : 'Proposed' : 'Your boundary'].map((label, i) => <div className="day-column" key={label}>
      <h3>{label}</h3><div className="time-track">
        {ordered.length === 0 && <div className="time-block original"><strong>—</strong><span>Nothing on your day yet</span></div>}
        {ordered.map(event => <div className={`time-block original ${i === 1 && proposal && event.id === current?.id ? 'moved' : ''}`} key={event.id}>
          <strong>{time(event.start, today.timezone)}</strong>
          <span>{event.title}</span>
          <small>{i === 1 && proposal && event.id === current?.id ? `Moving to ${time(proposal.start, today.timezone)}` : `${minutes(event)} minutes`}</small>
        </div>)}
        {i === 1 && proposal ? <div className={`time-block chosen ${confirmed ? 'confirmed' : ''}`}><strong>{time(proposal.start, today.timezone)}–{time(proposal.end, today.timezone)}</strong><span>{current?.title ?? 'This appointment'}</span><small>{proposal.venue.name}</small></div> : <div className="free-slot"><strong>15:00</strong><span>Room for a new plan</span></div>}
        <div className="buffer"><strong>{bufferStart}</strong><span>{boundary?.buffer_minutes ?? 0}-minute buffer, kept clear</span></div>
        <div className="protected-line"><strong>{boundaryTime}</strong><span>Protected</span></div>
        <div className="private-block">The time is shared.<br />The reason stays yours.</div>
      </div>
    </div>)}</div>
    {!compact && <ul className="day-legend">
      <li><span className="legend-swatch swatch-original" />On your day already</li>
      {proposal && <li><span className="legend-swatch swatch-chosen" />{confirmed ? 'Confirmed' : 'Proposed'}</li>}
      <li><span className="legend-swatch swatch-buffer" />Buffer, kept clear</li>
      <li><span className="legend-swatch swatch-protected" />Protected — never moves</li>
    </ul>}
    <p className="day-note"><span className="plum-dot" /> Your protected commitment never moves.</p>
  </section>;
}
