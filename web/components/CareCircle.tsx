'use client';
import { useState } from 'react';
import { CARE_KINDS, CARE_KIND_MEANING, type CareKind } from '@/lib/care';
import type { CareMember } from '@/lib/v2-types';

/**
 * Who may ever be told what about the person you care for.
 *
 * The screen has one sentence to make true, and it is not in the copy — it is in the shape:
 * **only the owner can add a member.** Three things carry it, in descending order of how hard they
 * are to argue with.
 *
 * 1. **`added_by` is a column, on every row, always reading "you".** It is not a tooltip and it is
 *    not a badge on the interesting rows: it is the same cell on all of them, because a field that
 *    can only ever say one thing is a proof and a field that sometimes says something else is a
 *    log. `POST /v2/care-circle` has deliberately no `added_by` on its body — the server writes it
 *    — so the column cannot come to disagree with the claim above it.
 * 2. **The form is the only way in, and it is on the owner's own screen.** There is no "approve
 *    this suggestion", no row an email can propose, no pending state. A product that could show a
 *    member half-added would be a product where something other than the owner had started the
 *    addition.
 * 3. **The refused paths are listed by name**, next to the list rather than in a help page, and
 *    they are the four `cannot` entries of limit clause 7 — the same strings the Limits screen
 *    shows and the same ones the decision row cites when a send is refused.
 *
 * The per-person grant is the other half. A circle where everyone may receive everything is an
 * address book; the question this screen answers is "who may be told about my mother", and the
 * answer is different for each of them.
 */
export function CareCircle({ members, unavailable, busy, owner, error, onAdd, onRemove }: {
  members: CareMember[];
  /** True when the server does not answer this route, or answered a 403. Never rendered as an
   *  empty circle: a missing list must not read as an absent restriction, which is the rule the
   *  gate itself follows — a build with no circle refuses every care recipient. */
  unavailable: boolean;
  busy: boolean;
  owner: boolean;
  error: string;
  onAdd: (value: { name: string; email: string; may_receive: string[] }) => void;
  onRemove: (member: CareMember) => void;
}) {
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [kinds, setKinds] = useState<CareKind[]>([]);
  const [confirming, setConfirming] = useState<string | null>(null);
  const toggle = (kind: CareKind) => setKinds(current => current.includes(kind) ? current.filter(k => k !== kind) : [...current, kind]);

  return <section className="care-page">
    <p className="lead">Who may ever be told something about Rosa, and what each of them may be told. Everyone on this list is here because you put them here.</p>

    {/* The load-bearing sentence, given the weight of the thing it is about: a bordered statement
        directly under the lead, before the list, not a note under it. */}
    <div className="owner-only" role="note">
      <h2>Only you can add someone to this circle.</h2>
      <p>Nothing can add someone to this list except you. Not an email, not the assistant, not a rule you taught it — there is no way for any of them to even ask.</p>
      <details className="owner-only-detail">
        <summary>The precise version</summary>
        <p>There is no path from an email, a model answer or a taught skill to this list. Not a refused one — an absent one: nothing in the product can call the route that adds a member, and the field recording who did is written by the server, never by the caller.</p>
      </details>
      <ul className="owner-only-cannot">
        <li>an address you did not add yourself</li>
        <li>a member added by an email, a model answer or a taught skill</li>
        <li>a member named for something you did not list them for</li>
        <li>a digest more than once a day</li>
      </ul>
      <p className="secondary">Those four are limit clause 7&rsquo;s own words, shown here and on Limits from the same source.</p>
    </div>

    {error && <p role="alert" className="alert error">{error}</p>}

    {unavailable
      ? <div className="card quiet-card"><h2>This server does not answer for a care circle.</h2><p>Nothing is listed, and that is not the same as nobody being listed. A care action with no circle to check against is refused, so an unreadable list is the safest state to be in and the least useful one to act on.</p></div>
      : <>
        <h2 className="section-title">{members.length} {members.length === 1 ? 'person' : 'people'}</h2>
        {members.length === 0 && <p className="secondary">Nobody yet. Until somebody is on this list, every care action is refused.</p>}
        <ul className="circle-list">{members.map(member => <li className="card circle-member" key={member.id}>
          <div className="cm-identity">
            <h3>{member.name}</h3>
            <p className="cm-address secondary">{member.email}</p>
          </div>
          <div className="cm-grants">
            <span className="cm-grants-label secondary">May receive</span>
            {member.may_receive.length === 0
              ? <span className="grant grant-none">Nothing yet</span>
              : <span className="grant-row">{member.may_receive.map(kind => <span className={`grant grant-${kind}`} key={kind}>{CARE_KIND_MEANING[kind as CareKind]?.title ?? kind}</span>)}</span>}
          </div>
          {/* The same cell on every row, and it can only ever say one thing. */}
          <div className="cm-added">
            <span className="cm-grants-label secondary">Added by</span>
            <span className="cm-added-by">{member.added_by === 'owner' ? 'You' : member.added_by}</span>
            <span className="secondary">{new Date(member.added_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })}</span>
          </div>
          <div className="cm-actions">
            {confirming === member.id
              ? <div className="button-row">
                <button className="danger" disabled={busy || !owner} onClick={() => { setConfirming(null); onRemove(member); }}>Remove {member.name.split(' ')[0]}</button>
                <button disabled={busy} onClick={() => setConfirming(null)}>Keep them</button>
              </div>
              : <button disabled={busy || !owner} onClick={() => setConfirming(member.id)}>Remove</button>}
            {confirming === member.id && <p className="secondary cm-remove-note">They stop receiving anything from now on. It does not unsend what already went out.</p>}
          </div>
        </li>)}</ul>
      </>}

    <form className="card care-add" onSubmit={event => {
      event.preventDefault();
      onAdd({ name, email, may_receive: kinds });
      setName(''); setEmail(''); setKinds([]);
    }}>
      <h2>Add someone</h2>
      <p className="secondary">This form is the only way onto the list.</p>
      <div className="form-row">
        <label>Their name<input value={name} onChange={e => setName(e.target.value)} required maxLength={120} autoComplete="off" /></label>
        <label>Their email<input type="email" value={email} onChange={e => setEmail(e.target.value)} required autoComplete="off" /></label>
      </div>
      <fieldset className="grant-picker">
        <legend>What may they receive?</legend>
        {CARE_KINDS.map(kind => <label className="grant-option" key={kind}>
          <input type="checkbox" checked={kinds.includes(kind)} onChange={() => toggle(kind)} />
          <span><strong>{CARE_KIND_MEANING[kind].title}</strong><span className="secondary">{CARE_KIND_MEANING[kind].body}</span></span>
        </label>)}
        {/* Empty is a real answer and the form says so, because a default here would be the product
            choosing a permission for the owner. */}
        <p className="secondary">Leaving all three unticked is allowed: it means &ldquo;on my list, trusted with nothing yet&rdquo;. Every care action to them is refused until you tick one.</p>
      </fieldset>
      <button className="primary" disabled={busy || !owner || !name.trim() || !email.trim()}>Add to the circle</button>
    </form>

    {!owner && <p className="attention">Sign in as the owner to read or change this circle. The list names the people in Rosa&rsquo;s life, so a read of it is a read of the family.</p>}
  </section>;
}
