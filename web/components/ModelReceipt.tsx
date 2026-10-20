'use client';
import type { ModelView } from '@/lib/v2-types';

/** Plain names for the categories `kept_on_machine` reports. The categories are NAMED and their
 *  values are absent — printing a protected title to prove it was safe would be the leak this panel
 *  exists to show missing. */
const categoryLabel: Record<string, string> = {
  title: 'The protected commitment’s title',
  location: 'Where it is',
  reason: 'Why it exists',
  attendees: 'Who else is on it',
  other_events: 'Every other meeting in your day',
};

/**
 * `null` and `0` are different answers and are drawn differently.
 * `0` is evidence: it was read, and it held nothing. `null` is a gap in the evidence: nothing was
 * ever read, so nothing can be claimed. A panel that rendered them the same would be asserting a
 * non-disclosure it never observed.
 */
function count(category: string, value: number | null | undefined) {
  if (value === null || value === undefined)
    return <span className="kept-count unknown">never read</span>;
  return <span className="kept-count">{value} kept here</span>;
}

export function ModelReceipt({ view, state }: { view: ModelView | null; state: 'loading' | 'ready' | 'forbidden' | 'unavailable' }) {
  if (state === 'loading')
    return <p className="secondary" role="status">Reading the model receipt…</p>;
  if (state === 'forbidden')
    return <p className="secondary">The prompts are the owner’s to read. This build is not signed in as the owner, so the receipt is not shown — that is the gate working, not an empty log.</p>;
  if (state === 'unavailable' || !view)
    return <p className="secondary">This server did not return a model receipt for this change. No claim is made about what was or was not sent.</p>;
  return <div className="receipt">
    {view.calls.length === 0
      ? <p className="secondary">No model was called for this change. In live mode every prompt that left is listed here, verbatim, with its tokens and its price.</p>
      : <ol className="call-list">{view.calls.map((call, index) => <li key={`${call.phase}-${index}`}>
        <div className="call-head">
          <strong>{call.phase}</strong>
          <span className="secondary">{call.tier} · {call.model}</span>
        </div>
        <p className="secondary tabular">{call.tokens.total} tokens · {call.cost_usd === null ? 'price unknown' : `$${call.cost_usd.toFixed(4)}`}</p>
        {call.withheld.length > 0 && <ul className="withheld">{call.withheld.map(row => <li key={row.field}><strong>{row.field}</strong> was kept back — {row.reason}</li>)}</ul>}
        {call.prompt_sent === null
          ? <p className="refused-inline">The gate refused this call. Nothing was sent.</p>
          : <details className="prompt"><summary>What was sent, verbatim</summary><pre>{JSON.stringify(call.prompt_sent, null, 2)}</pre></details>}
      </li>)}</ol>}
    <h4>Never left this machine</h4>
    <ul className="kept-list">
      {view.kept_on_machine.categories.map(category => <li key={category}>
        <span>{categoryLabel[category] ?? category}</span>
        {count(category, view.kept_on_machine.counts[category])}
      </li>)}
    </ul>
  </div>;
}
