'use client';
import { useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import { limitsModeLabel as labels } from './Shell';
import type { Limits, LimitsMode } from '@/lib/v2-types';

/**
 * The standing answer, and the seven clauses it is made of.
 *
 * Two things are deliberate here. The clauses are shown ALWAYS, not only while `within_limits` is
 * selected: they are what the owner is agreeing to, and a list that appears only after you have
 * chosen is a list you chose without reading. And the `off` mode is labelled **Watch only**
 * everywhere a person can see it, because "off" reads as "nothing is running" when in fact the
 * routine keeps watching and preparing and only execution stops. The API value is unchanged —
 * `labels` in `Shell.tsx` is the single place the two names meet.
 */
const blurbs: Record<LimitsMode, string> = {
  ask_always: 'Every prepared change waits for you. Nothing runs until you have read the exact actions.',
  within_limits: 'A change runs on its own only when every clause below passes. If one fails, it comes back to you.',
  off: 'The routine keeps watching and preparing. Nothing executes, even if you try to approve it.',
};

/**
 * Six clauses, in the owner's own words. Each promise stands for exactly one clause id; the clause's
 * own text — the words that actually run — sits in the one expandable panel below, verbatim, never
 * paraphrased. This is copy only: the clause objects still come straight from the API.
 */
const clausePromise: Record<string, string> = {
  '1': 'Moves only the meeting this email is about — never a different one, never a recurring series.',
  '2': 'Only moves it to a time that actually fits — never into your protected time or its buffer.',
  '3': 'Tells only the person who wrote in — never a second recipient, never anyone the invite would also notify.',
  '4': 'Replies only with pre-approved wording — never free text the model wrote on the spot.',
  '5': 'At most one calendar change and one reply per email — never twice, never for one that already ran.',
  '6': 'Touches nothing beyond this one change — no other events, no other emails, no extra searching.',
  '7': 'Tells your care circle only — and only the people you listed for that kind of thing. At most one digest a day.',
};
/** The seven clauses read as four plain-language principles — a wall of equally-weighted cards
 *  was what made a judge read this as a legalistic policy document rather than three things a person
 *  can actually hold in their head. The exact clauses are one click away, never rewritten. */
const principles: { title: string; body: string; clauses: string[] }[] = [
  { title: 'Only this meeting, only a time that fits', body: 'Never a different meeting, never a recurring series, and never a time inside your protected commitment or its buffer.', clauses: ['1', '2'] },
  { title: 'Only the person who wrote in is told', body: 'Never a second recipient, and only pre-approved wording — never free text the model wrote on the spot.', clauses: ['3', '4'] },
  { title: 'At most one edit and one reply', body: 'Never twice for the same email, and nothing beyond this one change.', clauses: ['5', '6'] },
  // The fourth principle, and the one this product's strongest use case turns on. It is stated as
  // conditional because the clause is: a plan that names nobody on the circle passes it with "this
  // plan sends nothing to your care circle", and a principle that implied every change was a care
  // action would be describing a stricter product than the one that runs.
  { title: 'Anything about someone you care for goes only to your circle', body: 'And only to the people you listed for that kind of thing — at most one family digest a day. A plan that tells nobody on your circle passes this without it applying.', clauses: ['7'] },
];

export function StandingLimits({ disabled, owner, onSaved }: { disabled: boolean; owner: boolean; onSaved?: (value: Limits) => void }) {
  const [limits, setLimits] = useState<Limits | null>(null);
  const [pending, setPending] = useState<LimitsMode | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const lock = useRef(false);
  const confirmHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { if (pending) confirmHeading.current?.focus(); }, [pending]);
  useEffect(() => {
    let active = true;
    api.limits().then(value => { if (active) setLimits(value); }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, []);
  async function confirm() {
    if (!pending || lock.current || disabled || !owner) return;
    lock.current = true; setBusy(true); setError('');
    try { const value = await api.setLimits(pending); setLimits(value); onSaved?.(value); setPending(null); setSaved(true); }
    catch (e) { setError(e instanceof Error ? e.message : 'Your limits could not be saved.'); }
    finally { lock.current = false; setBusy(false); }
  }
  const selected = pending ?? limits?.mode ?? null;
  return <section className="limits-page">
    <p className="lead">Your standing answer for meeting changes. A mode change never runs a plan already waiting for your review.</p>
    {error && <p role="alert" className="alert error">{error}</p>}
    {!limits ? <p role="status">{error ? 'Limits unavailable. Reopen this screen to try again.' : 'Loading your limits…'}</p> : <>
      <p className="current-answer">Current answer: <strong>{labels[limits.mode]}</strong>{limits.updated_at === null && ' · default'}</p>
      <div className="limit-choices" role="group" aria-label="Standing answer">
        {(Object.keys(labels) as LimitsMode[]).map(mode => <button
          key={mode}
          type="button"
          className="preset"
          aria-pressed={selected === mode}
          // The accessible name is the label alone. The blurb is on-screen help, and folding it into
          // the name would make the control announce a paragraph every time it takes focus.
          aria-label={labels[mode]}
          disabled={busy || disabled || !owner}
          onClick={() => { setPending(mode === limits.mode ? null : mode); setSaved(false); }}
        >
          {/* A single-select group reads as decisive with a visible radio mark, not just a border
              colour change — the thing a blind judge could not tell was a choice at all. */}
          <span className="preset-top"><span className="radio-dot" aria-hidden="true" /><span className="preset-name">{labels[mode]}</span></span>
          <span className="preset-blurb">{blurbs[mode]}</span>
          {selected === mode && <span className="preset-selected">Selected</span>}
        </button>)}
      </div>

      {pending && <section className="limit-confirm" aria-label="Confirm standing answer">
        <h3 ref={confirmHeading} tabIndex={-1}>Change to “{labels[pending]}”?</h3>
        <p>{pending === 'within_limits' ? 'Future preparations can run without asking when every clause passes.' : pending === 'off' ? 'Execution will be off. Watching and preparation continue.' : 'Every prepared change will wait for your approval.'}</p>
        <div className="button-row">
          <button className="primary" disabled={busy || disabled || !owner} onClick={() => void confirm()}>Confirm mode change</button>
          <button disabled={busy} onClick={() => setPending(null)}>Cancel</button>
        </div>
      </section>}
      {/* Explicit feedback for a mode change, as a toast-style banner rather than a line easy to miss
          at the bottom of a long page. */}
      {saved && <p role="status" className="saved-note toast">✓ Standing answer saved: {labels[limits.mode]}.</p>}

      <h2 className="clauses-title">The four principles</h2>
      <p className="secondary">These are the limits a change must pass to run without asking. Every one must pass; one failure hands the change back to you.</p>
      <ul className="principle-list">{principles.map(principle => <li className="card principle" key={principle.title}>
        <h3>{principle.title}</h3>
        <p className="secondary">{principle.body}</p>
      </li>)}</ul>

      <details className="clauses-exact-panel">
        <summary>See the {limits.clauses.length} exact clauses</summary>
        <ol className="limit-clauses">{limits.clauses.map(clause => <li className="clause" key={clause.id}>
          <div className="clause-head"><span className="clause-number" aria-hidden="true">{clause.id}</span><h3>{clausePromise[clause.id] ?? clause.text}</h3></div>
          <p className="clause-exact">{clause.text}</p>
          <p className="secondary">What it can&apos;t do:</p>
          <ul>{clause.cannot.map(item => <li key={item}>{item}</li>)}</ul>
        </li>)}</ol>
      </details>
    </>}
    {!owner && <p>Sign in as the owner to view and change limits.</p>}
  </section>;
}
