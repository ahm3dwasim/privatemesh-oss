'use client';
import { useState } from 'react';
import { FACT_KINDS, FACT_KIND_LABEL, factPlaceholder, type FactKind } from '@/lib/care';
import type { ProtectedFact } from '@/lib/v2-types';

/**
 * What must never leave — as labels, and only ever as labels.
 *
 * **There is no place on this screen where a value could be displayed, and that is a property of
 * the code rather than a promise in the copy.** `ProtectedFact` has no `value` field, `GET` does
 * not return one, `POST` does not echo one back, and the sample adapter throws its input away
 * after validating it rather than keeping a copy it could later be asked for. So a future screen
 * that wanted to show a value would have to add a route first — the thing this feature exists to
 * prevent — instead of reading a field that was sitting there.
 *
 * The value is typed exactly once, into the form below, and the form's own field is cleared in the
 * same turn it is submitted. The row that comes back is a label, a kind and whether the masker has
 * had to use it.
 *
 * What "protected" MEANS is the other half of this screen, and it is stated as the two rules the
 * gate actually runs rather than as reassurance: never in a model prompt, and never in an outbound
 * artifact unless the owner approved those exact bytes for people on the care circle.
 */
export function ProtectedFacts({ facts, unavailable, busy, owner, error, onAdd, onRemove }: {
  facts: ProtectedFact[];
  unavailable: boolean;
  busy: boolean;
  owner: boolean;
  error: string;
  onAdd: (value: { label: string; kind: string; value: string }) => void;
  onRemove: (fact: ProtectedFact) => void;
}) {
  const [label, setLabel] = useState('');
  const [kind, setKind] = useState<FactKind>('benefits_id');
  const [value, setValue] = useState('');
  const [confirming, setConfirming] = useState<string | null>(null);

  return <section className="care-page">
    <p className="lead">Four things about Rosa that must never leave this machine because something here <em>read</em> them. You typed each one once; nothing has been able to read one back since.</p>

    <div className="protection-means">
      <h2>What protection means, exactly</h2>
      <ol className="means-list">
        <li>
          <strong>It never enters a model prompt.</strong>
          <p>Every value is replaced by its placeholder immediately before the request body is built, and the ordinary egress guard then runs on the bytes that would actually be posted. Two steps, not one: if the masking ever misses a value, the guard raises and nothing is sent — so a masking bug fails as a refusal, not as a leak.</p>
        </li>
        <li>
          <strong>It never appears in anything outbound</strong> — unless you approved <em>that exact artifact</em> containing it, and every recipient is on your care circle.
          <p>The check runs on the final bytes of the composed body, after every fragment is rendered, because a check on the pieces is a check a later composition step can walk around.</p>
        </li>
        <li>
          <strong>A refusal is recorded without repeating the value.</strong>
          <p>The decision row names the label and counts the audience — &ldquo;the protected Benefits id would have been disclosed to 2 recipient(s)&rdquo;. An address in a receipt is a recipient list in a log, and a log gets exported and pasted into bug reports.</p>
        </li>
      </ol>
    </div>

    {error && <p role="alert" className="alert error">{error}</p>}

    {unavailable
      ? <div className="card quiet-card"><h2>This server does not answer for protected facts.</h2><p>Nothing is listed. That is not the same as nothing being protected, and it is not the same as nothing being at risk — it means this build cannot tell you either way.</p></div>
      : <>
        <h2 className="section-title">{facts.length} protected {facts.length === 1 ? 'fact' : 'facts'}</h2>
        <ul className="fact-list">{facts.map(fact => <li className="card fact-row" key={fact.id}>
          <div className="fact-identity">
            <h3>{fact.label}</h3>
            {/* The kind is safe to render verbatim: it is a category, not a secret. The placeholder
                beside it is what a model is shown in place of the value — a NAME it can reason
                with and cannot read anything out of. */}
            <p className="fact-kind"><span className="kind-chip">{FACT_KIND_LABEL[fact.kind as FactKind] ?? fact.kind}</span> <code className="placeholder">{factPlaceholder(fact.kind)}</code></p>
          </div>
          <div className="fact-state">
            {fact.in_use
              ? <><span className="in-use">In use</span><span className="secondary">The masker has replaced this value in a model payload.</span></>
              : <><span className="not-used secondary">Not needed yet</span><span className="secondary">Nothing has had to mask this one.</span></>}
          </div>
          <div className="fact-actions">
            {confirming === fact.id
              ? <div className="button-row">
                <button className="danger" disabled={busy || !owner} onClick={() => { setConfirming(null); onRemove(fact); }}>Stop protecting it</button>
                <button disabled={busy} onClick={() => setConfirming(null)}>Keep it protected</button>
              </div>
              : <button disabled={busy || !owner} onClick={() => setConfirming(fact.id)}>Remove</button>}
            {confirming === fact.id && <p className="secondary">From then on the gate has nothing to match, so this value could appear in a prompt or a reply. It cannot retract anything already refused or already sent.</p>}
          </div>
        </li>)}</ul>
        {facts.length === 0 && <p className="secondary">Nothing is protected yet. The gate matches on values it has been given, so an empty list means it has nothing to look for.</p>}
      </>}

    <form className="card fact-add" onSubmit={event => {
      event.preventDefault();
      onAdd({ label, kind, value });
      // Cleared in the same turn it is submitted. The field is the only place in this app a
      // protected value is ever held, and it is not held after this line.
      setLabel(''); setValue('');
    }}>
      <h2>Protect something else</h2>
      <p className="secondary">You type the value once, here. It is stored on this machine for the masker and the gate to match on, and no route serves it back — not to this screen, not to any other.</p>
      <div className="form-row">
        <label>What to call it<input value={label} onChange={e => setLabel(e.target.value)} required maxLength={80} autoComplete="off" placeholder="Benefits id" /></label>
        <label>Kind<select value={kind} onChange={e => setKind(e.target.value as FactKind)}>{FACT_KINDS.map(k => <option key={k} value={k}>{FACT_KIND_LABEL[k]}</option>)}</select></label>
      </div>
      <label>The value<input type="password" value={value} onChange={e => setValue(e.target.value)} required autoComplete="off" /></label>
      <p className="secondary">A model asked about this will be shown <code className="placeholder">{factPlaceholder(kind)}</code> instead — enough to reason with (&ldquo;the form wants something I cannot read&rdquo;) and impossible to read anything out of.</p>
      <button className="primary" disabled={busy || !owner || !label.trim() || !value.trim()}>Protect it</button>
    </form>

    {!owner && <p className="attention">Sign in as the owner to read this list.</p>}
  </section>;
}
