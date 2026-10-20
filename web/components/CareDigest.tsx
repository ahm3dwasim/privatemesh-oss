'use client';
import { artifactId, audience, DIGEST_SECTIONS, shortArtifactId } from '@/lib/care';
import type { CareDigestAction, CareMember } from '@/lib/v2-types';

/**
 * The family digest, on the ordinary review screen.
 *
 * `docs/API_V2.md` is explicit that this is not a bespoke screen: the digest surfaces through the
 * ordinary change shape and "a bespoke screen would be a second place for the same decision". So
 * this is a card inside `ChangeDetail`, in the slot the proposed calendar edit and reply occupy
 * for a scheduling change, and the approve button below it is the same button.
 *
 * What it has to make legible is the one thing a rendered email cannot: **approving is approving
 * THESE BYTES.** `artifact_id` is a sha256 over the day, the recipients and every fragment in
 * order, so the name moves when a word does. The screen therefore recomputes it — from the
 * fragments it is about to draw, not from the field the server sent — and shows the two side by
 * side when they differ. A screen that printed the server's id under the server's text would be
 * showing a label, and a label is exactly what content-addressing exists instead of.
 */
export function CareDigest({ digest, members, approvedArtifact, onEdit, busy, owner }: {
  digest: CareDigestAction;
  /** The circle, so the audience can be checked HERE rather than asserted. A recipient who is a
   *  member but is not listed for `digest` is outside, and the screen has to say so: the grant is
   *  per-kind, and membership alone clearing a send is the mistake clause 7 names. */
  members: CareMember[];
  /** The artifact the owner last approved on this change, if any. When it does not match the one
   *  on screen, the approval is stale in the only sense that matters. */
  approvedArtifact?: string | null;
  onEdit?: (edit: { digest_keep_records?: string[]; digest_recipients?: string[] }) => void;
  busy: boolean;
  owner: boolean;
}) {
  // Recomputed from the bytes being rendered. If this disagrees with what the server sent, the
  // screen says so rather than picking one — two answers to "what is this artifact called" is a
  // state the owner needs to see, not one for the client to resolve quietly.
  const computed = artifactId({ day: digest.day, recipients: digest.to, fragments: digest.fragments });
  const agrees = computed === digest.artifact_id;
  const { inside, outside, approvable } = audience(digest.to, members, 'digest');
  const staleApproval = !!approvedArtifact && approvedArtifact !== computed;
  const byIndex = new Map(digest.discloses.map(d => [d.fragment, d]));

  return <section className="card digest" aria-label="The family digest">
    <div className="digest-head">
      {/* Not the page's own title again. The heading names what the card is FOR — these are the
          bytes an approval covers — and the page above it already says which digest this is. */}
      <h2>The exact words</h2>
      <p className="secondary">Composed from your own records &mdash; every line below is the render of a calendar event, a change or a message this machine already holds. No model wrote any of it, and a line whose words are not that record&rsquo;s own render is refused rather than labelled.</p>
    </div>

    {/* The bytes. Sections in the order they will be sent, because "in order" is part of the
        artifact's identity and a reordered digest is a different digest. */}
    <ol className="digest-body">
      {DIGEST_SECTIONS.map(section => {
        const lines = digest.fragments.map((fragment, index) => ({ fragment, index })).filter(row => row.fragment.source === section.source);
        if (lines.length === 0) return null;
        return <li className="digest-section" key={section.source}>
          <h3>{section.heading}</h3>
          <ul>{lines.map(({ fragment, index }) => {
            const disclosed = byIndex.get(index);
            return <li className={`digest-line${disclosed ? ' discloses' : ''}`} key={`${fragment.source}-${index}`}>
              <span className="digest-text">{disclosed ? highlight(fragment.text, disclosed.value) : fragment.text}</span>
              <span className="digest-provenance secondary">{fragment.record_id ? `from ${fragment.record_id}` : 'the composer’s own opening line'}</span>
              {disclosed && <span className="digest-fact-chip">Protected &middot; {disclosed.label}</span>}
              {/* No "leave this out" on the greeting: it is the composer's own opening line, it
                  names no record, and a digest without it is not a shape the composer can build.
                  An edit control for something the product would refuse is a control that teaches
                  the wrong thing about what the owner is in charge of. */}
              {onEdit && fragment.source !== 'care_greeting' && digest.fragments.length > 2 && <button className="text-button digest-drop" disabled={busy || !owner} onClick={() => onEdit({ digest_keep_records: digest.fragments.filter(f => f !== fragment && f.source !== 'care_greeting').map(f => f.record_id) })}>Leave this line out</button>}
            </li>;
          })}</ul>
        </li>;
      })}
    </ol>

    {/* The audience, with the grant each person holds — not a recipient list, because the question
        the owner is answering is "may this person be told this kind of thing". */}
    <div className="digest-audience">
      <h3>Going to</h3>
      <ul>{inside.map(row => <li key={row.email}>
        <strong>{members.find(m => m.email.toLowerCase() === row.email.toLowerCase())?.name ?? row.email}</strong>
        <span className="secondary">{row.email}</span>
        <span className="grant grant-digest">listed for the digest</span>
        {onEdit && inside.length > 1 && <button className="text-button" disabled={busy || !owner} onClick={() => onEdit({ digest_recipients: digest.to.filter(address => address !== row.email) })}>Leave them out</button>}
      </li>)}</ul>
      {outside.length > 0 && <p className="alert error" role="alert"><strong>{outside.join(', ')}</strong> {outside.length === 1 ? 'is' : 'are'} not on your care circle for a digest. Nothing can be sent to this audience, and only you can change that &mdash; on the Care circle screen.</p>}
    </div>

    {/* The disclosure notice. It exists because one line carries a value from the protected list,
        and the owner is the only party who can say that those exact bytes may go to those exact
        people. */}
    {digest.discloses.length > 0 && <div className={`disclosure-notice${approvable ? '' : ' disclosure-blocked'}`} role="note">
      <h3>This digest discloses {digest.discloses.length === 1 ? 'a protected fact' : `${digest.discloses.length} protected facts`}.</h3>
      <ul>{digest.discloses.map(row => <li key={row.label}><strong>{row.label}</strong> appears in the highlighted line above.</li>)}</ul>
      <p>{approvable
        ? <>Approving sends those exact bytes to {inside.length} {inside.length === 1 ? 'person' : 'people'}, each on your care circle and each listed for the digest. Approving it for them does not approve it for anyone else: a later digest with a different audience is a different artifact and needs its own approval.</>
        : <>It cannot be approved while anyone on the audience is outside your circle. Approving a field for one recipient does not approve it for another, so a digest to two people where one is listed is a disclosure to the other.</>}</p>
    </div>}

    {/* The content address. Shown in full, because the property being demonstrated is that two of
        these can be compared. */}
    <div className="artifact" aria-label="What approving covers">
      <span className="artifact-label secondary">Approving covers exactly this artifact</span>
      <code className="artifact-id" title={computed}>{computed}</code>
      <p className="secondary">A sha256 over the day, the recipients and every line in order, each with the record it came from. Change a word, a recipient or the day and this name changes &mdash; which is how an approval stops matching, instead of quietly carrying over to whatever the digest says later.</p>
      {!agrees && <p className="alert error" role="alert"><strong>This is not the artifact the server named.</strong> The server sent <code>{shortArtifactId(digest.artifact_id)}</code> and these bytes are <code>{shortArtifactId(computed)}</code>. Do not approve it: one of the two is describing something other than what is on this screen.</p>}
      {staleApproval && <p className="alert error" role="alert"><strong>Your earlier approval no longer matches.</strong> You approved <code>{shortArtifactId(approvedArtifact!)}</code>. This digest is <code>{shortArtifactId(computed)}</code>, so that approval covers bytes that are not these. Read it again before approving.</p>}
    </div>
  </section>;
}

/** The one place in this app a protected value is rendered, marked where it sits. Split on the
 *  exact substring rather than matched loosely: a fuzzy highlight over a secret is a second way to
 *  be wrong about where it is. */
function highlight(text: string, value: string) {
  const index = text.indexOf(value);
  if (index < 0) return text;
  return <>{text.slice(0, index)}<mark className="protected-span">{value}</mark>{text.slice(index + value.length)}</>;
}
