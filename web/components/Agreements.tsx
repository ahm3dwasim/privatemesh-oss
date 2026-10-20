'use client';
import type { Agreement, AgreementDrift } from '@/lib/v2-types';
import { time } from './Day';

/**
 * Agreements: the meetings the assistant set up, and whether the calendar still says what the owner
 * agreed to.
 *
 * An approval is a statement about one bundle at one moment. Anything can move it afterwards —
 * including a person with calendar access who is not the owner — and no other screen in this product
 * ever goes back to look. This one does, and it is read-only: it reports drift, it never repairs it.
 *
 * Changed agreements lead, because they are the reason this screen exists. Unchecked ones follow,
 * each with the one reason it hasn't been looked at and the one action that fixes that. Healthy
 * agreements — the calendar still matches, and it is most of them on a quiet week — are compacted
 * into single rows: repeating the full card for every one of them was what made "the calendar
 * matches" read as loud as "the calendar changed", the opposite of what the page is for.
 */
const fieldLabel: Record<string, string> = { start: 'Starts', end: 'Ends', location: 'Place', attendees: 'Attendees' };

const value = (input: string | string[], timezone: string, field: string) => {
  if (Array.isArray(input)) return input.join(', ');
  if ((field === 'start' || field === 'end') && Number.isFinite(Date.parse(input))) return time(input, timezone);
  return input;
};

function DriftRow({ row, timezone }: { row: AgreementDrift; timezone: string }) {
  const added = Array.isArray(row.observed) && Array.isArray(row.agreed)
    ? row.observed.filter(entry => !row.agreed.includes(entry))
    : [];
  return <div className="drift-row">
    <dt>{fieldLabel[String(row.field)] ?? String(row.field)}</dt>
    {/* Agreed and observed as two labelled lines, not one run-together sentence of strikethroughs,
        an arrow and a warning pill — the density a judge had to untangle field by field. */}
    <dd>
      <span className="drift-was"><span className="drift-sub">You agreed</span> <span className="agreed">{value(row.agreed, timezone, String(row.field))}</span></span>
      <span className="drift-now"><span className="drift-sub">Calendar now says</span> <strong className="observed">{value(row.observed, timezone, String(row.field))}</strong></span>
      {added.length > 0 && <span className="drift-note">{added.length === 1 ? '1 person' : `${added.length} people`} you did not agree to</span>}
    </dd>
  </div>;
}

export function Agreements({ agreements, timezone, busy, unavailable, onCheck, onOpen }: {
  agreements: Agreement[];
  timezone: string;
  busy: boolean;
  unavailable: boolean;
  onCheck: () => void;
  onOpen: (changeId: string) => void;
}) {
  const drifted = agreements.filter(a => a.status === 'drifted');
  const unchecked = agreements.filter(a => a.status === 'unchecked');
  const holding = agreements.filter(a => a.status === 'holding');
  return <section className="agreements-page" aria-label="Agreements">
    <div className="agreements-head">
      <p>{unavailable
        ? 'This server does not report agreements yet. Nothing is claimed about the meetings below.'
        : drifted.length === 0
          ? `Every meeting the assistant set up still matches what you agreed to — as far as it has looked.${unchecked.length ? ` ${unchecked.length} ${unchecked.length === 1 ? 'has' : 'have'} not been checked yet.` : ''}`
          : `${drifted.length} ${drifted.length === 1 ? 'meeting has' : 'meetings have'} changed since you agreed.`}</p>
      {!unavailable && agreements.length > 0 && <button disabled={busy} onClick={onCheck}>Check the calendar now</button>}
    </div>

    {unavailable || agreements.length === 0
      ? <div className="card quiet-card"><h2>No agreements yet.</h2><p>When you approve a change, the meeting it sets up is listed here and checked against the calendar afterwards.</p></div>
      : <>
        {drifted.length > 0 && <ul className="agreement-list">
          {drifted.map(agreement => <li key={agreement.id} className="card agreement drifted">
            <div className="agreement-head">
              <h2>{agreement.title}</h2>
              <span className="status-chip tone-refused">Changed since you agreed</span>
            </div>
            <p className="secondary tabular">You agreed: {time(agreement.agreed.start, timezone)}–{time(agreement.agreed.end, timezone)} · {agreement.agreed.location} · {agreement.agreed.attendees.join(', ')}</p>
            <dl className="drift">{agreement.drift.map(row => <DriftRow key={String(row.field)} row={row} timezone={timezone} />)}</dl>
            <p className="secondary">The assistant did not make these changes and will not undo them. It only tells you.</p>
            <div className="agreement-foot">
              <span className="secondary">{agreement.checked_at ? `Checked ${time(agreement.checked_at, timezone)}` : 'Never checked'}</span>
              <div className="button-row">
                <button className="primary text-button" onClick={() => onOpen(agreement.change_id)}>Review this change</button>
              </div>
            </div>
          </li>)}
        </ul>}

        {/* Unchecked, like holding, is a compact row — the reason it hasn't been looked at is stated
            ONCE for the group, not repeated as a full card of boilerplate on every one of them. Only
            a drifted agreement, the reason this screen exists, earns the expanded card above. */}
        {unchecked.length > 0 && <section className="agreement-unchecked-group">
          <p className="unchecked-note">{unchecked.length} {unchecked.length === 1 ? 'meeting has' : 'meetings have'} not been checked against the calendar yet. This is not a problem — press "Check now" to compare it against what was agreed.</p>
          <ul className="agreement-list compact">
            {unchecked.map(agreement => <li key={agreement.id} className="agreement agreement-row unchecked">
              <span className="status-chip tone-quiet">Not checked yet</span>
              <span className="agreement-row-title">{agreement.title}</span>
              <span className="secondary tabular">{time(agreement.agreed.start, timezone)}–{time(agreement.agreed.end, timezone)}</span>
              <span className="secondary">Never checked</span>
              <button className="primary text-button" disabled={busy} onClick={onCheck}>Check now</button>
            </li>)}
          </ul>
        </section>}

        {holding.length > 0 && <details className="agreement-healthy">
          <summary>{holding.length} {holding.length === 1 ? 'meeting is' : 'meetings are'} holding as agreed</summary>
          <ul className="agreement-list compact">
            {holding.map(agreement => <li key={agreement.id} className="agreement agreement-row holding">
              <span className="status-chip tone-done">Holding</span>
              <span className="agreement-row-title">{agreement.title}</span>
              <span className="secondary tabular">{time(agreement.agreed.start, timezone)}–{time(agreement.agreed.end, timezone)}</span>
              <span className="secondary">{agreement.checked_at ? `Checked ${time(agreement.checked_at, timezone)}` : 'Never checked'}</span>
              <button className="text-button" onClick={() => onOpen(agreement.change_id)}>View original approval</button>
            </li>)}
          </ul>
        </details>}
      </>}
  </section>;
}
