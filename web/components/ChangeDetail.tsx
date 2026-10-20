'use client';
import type { ReactNode } from 'react';
import type { CareMember, Change, ChangeSummary, Decision, EffectKind, Limits, ModelView, Outcome, Today } from '@/lib/v2-types';
import { CareDigest } from './CareDigest';
import { Refusal } from './Refusal';
import { DecisionLog, EmailInput, RefusalCard } from './Decisions';
import { ModelReceipt } from './ModelReceipt';
import { OverreachList } from './Learning';
import { Day, time } from './Day';
import { effectSentence, subjectOf } from '@/lib/feed';
import { safeHttpUrl } from '@/lib/urls';

/**
 * Decision first, proof on demand.
 *
 * The page opens with one canonical summary in plain language — what was asked, what this bundle
 * does — and the proposed change, what it left out, and the one action that approves it sit right
 * under it, sticky on a phone. There is no separate "review" click: the exact calendar diff and the
 * exact reply are shown here, before the button, and pressing it is the approval. Everyone reads
 * this far. The trust boundary this product is actually built on —
 *
 *   what ARRIVED  →  what the MODEL proposed  →  what TRUSTED CODE checked
 *
 * — is still the whole story underneath, and it is still drawn the same way: the untrusted columns
 * recessed and dashed, the trusted one raised, because an email is a request and a model's answer is
 * a suggestion and only trusted code can cause anything to happen. It just is not the FIRST thing the
 * page says any more. It lives one click away, in "How this was checked" — expanded, it is meant to
 * look exactly as considered as it did when it was the whole page, because it is the shot this
 * product is proudest of. The technical words that used to sit in plain view — untrusted, trusted
 * code, validator, allowlisted — live only inside it now.
 */
function Source({ evidence }: { evidence: NonNullable<Change['proposal']>['venue']['evidence'] }) {
  // The scheme is checked before the string reaches an `href`. An `href` is a place a scheme
  // executes, not a place a string is displayed: a `javascript:` URL runs with this page's origin,
  // inside the view that is showing the owner their private data. Only http(s) renders a link at
  // all, and the excerpt above it is the evidence either way — a rejected URL is a link that is not
  // drawn, never a link drawn with a value nobody checked.
  const href = safeHttpUrl(evidence.url);
  return <div className="source">
    <strong>{evidence.sample ? 'Sample source' : evidence.official ? 'Official source' : 'Public source'} · {evidence.title}</strong>
    <p>{evidence.excerpt}</p>
    <small>Checked {new Date(evidence.retrieved_at).toLocaleString('en-GB', { timeZone: 'Europe/London' })} · Europe/London</small>
    {href && <a href={href} target="_blank" rel="noreferrer">Read source</a>}
  </div>;
}

export interface ChangeDetailProps {
  today: Today;
  change: Change;
  summary?: ChangeSummary;
  decisions: { records: Decision[]; unavailable: boolean } | null;
  clauses: Limits['clauses'];
  outcome: Outcome | null;
  modelView: ModelView | null;
  modelViewState: 'loading' | 'ready' | 'forbidden' | 'unavailable';
  stale: boolean;
  expired: boolean;
  validBundle: boolean;
  owner: boolean;
  busy: boolean;
  offline: boolean;
  sample: boolean;
  editing: boolean;
  waited: boolean;
  preparing: boolean;
  executing: boolean;
  onBack: () => void;
  onPrepare: () => void;
  onApprove: () => void;
  onLeave: () => void;
  onStartEdit: () => void;
  onWhy: () => void;
  editForm?: ReactNode;
  learning?: ReactNode;
  afterActions?: ReactNode;
  /** The care circle, so a digest's audience can be CHECKED on this screen rather than asserted
   *  by it. Empty when the server does not answer for one, which the digest card renders as an
   *  audience it cannot clear — never as an audience that is fine. */
  circle?: CareMember[];
  /** The corpus's own measured answers for a refused change, quoted on the refusal screen. */
  checks?: { check: string; failed: boolean; why: string }[];
  /** The artifact the owner last approved on this change, when it was a digest. */
  approvedArtifact?: string | null;
  onEditDigest?: (edit: { digest_keep_records?: string[]; digest_recipients?: string[] }) => void;
}

const stageNames: Record<string, string> = {
  reading_thread: 'Reading the selected thread',
  checking_availability: 'Checking your availability',
  checking_venues: 'Checking venue information',
  approved: 'Your two actions are approved',
  executing: 'Putting your plan in place',
};
const preparingStages = ['reading_thread', 'checking_availability', 'checking_venues'];

/** A provider record link, or nothing. Named so the two call sites read the same and so a third one
 *  added later has an obvious function to call instead of an `href` to fill in. */
const providerHref = (url: string | undefined) => safeHttpUrl(url);

/** One receipt line per effect, always in the tense a finished action actually happened in. Sample
 *  and live read the SAME past-tense receipt — the honesty gap between them is stated once, at the
 *  top of the card ("Simulated"), never re-litigated line by line as "would" versus "did". */
function effectLabel(kind: EffectKind, state: string) {
  const noun = kind === 'calendar_update' ? 'Calendar update' : kind === 'care_digest' ? 'Family digest' : 'Email reply';
  if (state === 'done')
    return kind === 'calendar_update' ? 'Calendar updated' : kind === 'care_digest' ? 'Digest sent' : 'Reply sent';
  return `${noun} · ${state}`;
}

export function ChangeDetail(props: ChangeDetailProps) {
  const { today, change, summary, decisions, clauses, outcome, stale, expired, validBundle, owner, busy, offline, sample, editing, waited, preparing, executing } = props;
  const proposal = change.proposal;
  const mail = proposal?.actions.find(a => a.kind === 'email_reply');
  const calendar = proposal?.actions.find(a => a.kind === 'calendar_update');
  const digestAction = proposal?.actions.find(a => a.kind === 'care_digest');
  const digest = digestAction?.kind === 'care_digest' ? digestAction : null;
  const finished = change.stage === 'done' || change.stage === 'partial';
  const records = decisions?.records ?? [];
  const refusal = records.find(d => d.verdict === 'refused');
  // Whether the decision log is a gap in the evidence rather than an empty result. Every heading
  // below that would otherwise say "what trusted code checked" has to answer for this: a heading is
  // an affirmative claim, and "What trusted code checked" over a log that never loaded tells the
  // owner a check passed without anything having been read. `DecisionLog` already says so in its own
  // body; the titles have to stop contradicting it before anyone reads that far.
  const checksUnread = decisions?.unavailable === true;
  const threadHref = safeHttpUrl(summary?.thread_url);
  const clauseRows = records.filter(d => d.stage === 'approve' && d.source === 'owner' && clauses.some(c => c.text === d.rule));
  const handback = clauseRows.filter(d => d.verdict === 'refused');

  // The three lines a person reads before anything else: who asked, what this bundle actually does
  // (named, not "2 actions"), and what — if anything — it refused along the way. All three are true
  // regardless of stage: "what this does" describes the bundle's content, not a claim that it ran.
  const who = summary?.from_name ?? 'Someone';
  // A change that was REFUSED gets the refusal screen in place of the plan, because the four
  // questions a person has about a stopped attack are not the three a plan answers. The condition
  // is a terminal stage plus a refusal on the log (or the change's own count of stopped requests
  // when the log did not load) — never the stage alone, which is also how a day with no free slot
  // ends, and a scheduling inconvenience drawn as a security event is as wrong as the reverse.
  // A refusal at the SEND stage is the only one that means "this change produced nothing because
  // the words would have left and did not". Any refused row would be too loose a test: the
  // ordinary scheduling change in this fixture also refuses an injected tail, at `validate`, and
  // then goes on to produce a perfectly good plan — a change that was narrowed on the way is not
  // a change that was stopped. If the log did not load there is no refusal screen, because the
  // screen is made of the log: the change's own `blocked_reason` renders instead, and it says
  // what happened without this component asserting which check said so.
  const refused = change.stage === 'no_safe_fit' && records.some(d => d.verdict === 'refused' && (d.stage === 'send' || d.stage === 'execute'));
  const askedLine = digest
    ? 'Nothing arrived. This was composed from your own records.'
    : summary ? `${who} asked to change this appointment.` : 'A request to move an appointment arrived by email.';
  const namedActions = digest
    ? `Send today’s family digest to ${digest.to.length} ${digest.to.length === 1 ? 'person' : 'people'} on your care circle.`
    : calendar?.kind === 'calendar_update' && mail?.kind === 'email_reply'
      ? `Move it to ${time(calendar.diff.start[1], today.timezone)}–${time(calendar.diff.end[1], today.timezone)} at ${calendar.diff.location[1]}, and reply to ${who}.`
      : null;

  // The outcome's own concise receipt: what each effect reported, and that the protected boundary
  // held regardless — read before the itemized detail below them.
  //
  // It is built from `outcome.effects`, one sentence per effect, from that effect's OWN state. The
  // version this replaces read the change's STAGE and printed "The reply did not send, so nothing
  // else was told" for every `partial` — including the case where the reply's delivery was never
  // confirmed, where that sentence asserts as fact something nothing observed. `effectSentence` is
  // the same function the Today row uses, so the two surfaces cannot come to describe one effect
  // differently again.
  const receiptLine = finished
    ? [
      outcome
        ? outcome.effects.map(effect => effectSentence(effect, { who, when: calendar?.kind === 'calendar_update' ? `${time(calendar.diff.start[1], today.timezone)}–${time(calendar.diff.end[1], today.timezone)}` : 'an unavailable time', where: calendar?.kind === 'calendar_update' ? calendar.diff.location[1] : 'an unavailable venue', what: subjectOf(change, who) })).join(' ')
        : 'The recorded result of this change has not been read, so this line claims nothing about its effects.',
      proposal?.protected ? `Your ${time(proposal.protected.start, today.timezone)} boundary held.` : null,
    ].filter(Boolean).join(' ')
    : null;

  // Sample mode never calls a model — there is no provider, no key and no network in it. Saying
  // "What the model proposed" over a proposal no model produced is the contradiction a judge read as
  // dishonest even after the panel explained itself; naming it for what it is removes the
  // contradiction before anyone has to read past the heading.
  const col2Title = sample ? 'Stored sample proposal' : 'What the model proposed';

  return <div className="detail">
    <div className="detail-top">
      <button type="button" className="ghost back" onClick={props.onBack}>← Today</button>
    </div>

    {refused && <Refusal
      request={change.request_excerpt}
      sender={who}
      senderAddress={summary?.from_address ?? 'an address this build could not read'}
      decisions={records}
      blockedAttempts={change.blocked_attempts}
      checks={props.checks ?? []}
      circle={props.circle ?? []}
      reason={change.blocked_reason}
    />}

    {!refused && <section className="decision-summary" aria-label="Summary">
      <p className="ds-line ds-asked">{askedLine}</p>
      {summary && <p className="ds-line ds-when secondary">{summary.from_name} · {time(summary.received_at, today.timezone)}</p>}
      {namedActions
        ? <p className="ds-line ds-will"><span className="ds-tag">Proposed change</span> {namedActions}</p>
        : <p className="ds-line secondary">Nothing has been proposed yet.</p>}
      {/* One clear security notice, never the refusal's own phrase repeated under a "Refused" tag —
          that was reading as "Refused Refused: …". What exactly was refused, and why, is one click
          away in "How this was checked"; here the owner only needs to know the safe plan still needs
          them regardless of what was blocked getting to it. */}
      {refusal && <p className="ds-line ds-blocked"><span className="ds-tag ds-tag-blocked">Security</span> Unsafe part blocked — the safe plan still needs you.</p>}
    </section>}

    <div className="detail-actions">
      {change.stage === 'detected' && <section className="card next-step">
        <h2>You choose what happens next.</h2>
        <p>I'll check a new venue and a time that leaves your protected commitment untouched.</p>
        <button className="primary full" disabled={busy} onClick={props.onPrepare}>Handle this change</button>
        <p className="secondary">Preparation follows your standing answer: ask every time by default, act only within your limits, or watch only.</p>
      </section>}

      {(preparing || executing) && <section className="card" aria-live="polite">
        <span className="status-chip tone-working">{preparing ? 'Preparing your plan' : 'Approved plan'}</span>
        <h2>{stageNames[change.stage]}</h2>
        {preparing && <ol className="stages">{preparingStages.map((stage, index) => <li key={stage} className={preparingStages.indexOf(change.stage) >= index ? 'active' : ''}><span>{preparingStages.indexOf(change.stage) > index ? '✓' : index + 1}</span>{stageNames[stage]}</li>)}</ol>}
        {waited && <p className="attention">Still {stageNames[change.stage]?.toLowerCase()}. No actions have been approved. You can leave this open or cancel preparation.</p>}
        {preparing && <button disabled={busy} onClick={props.onLeave}>Cancel preparation</button>}
      </section>}

      {change.stage === 'no_safe_fit' && !refused && <section className="card">
        <span className="status-chip tone-attention">No safe fit</span>
        <h2>There isn't a plan inside these boundaries.</h2>
        <p>{change.blocked_reason ?? '16:00 runs into your protected buffer. The other venues do not have supported opening hours.'}</p>
        <p>Your {time(today.protected[0]?.start ?? '', today.timezone)} commitment and {today.protected[0]?.buffer_minutes ?? 0}-minute buffer stay fixed. Which flexible part would you like to change?</p>
        <div className="button-row">
          <button onClick={props.onBack}>Back to Today</button>
          <button disabled={busy} onClick={props.onLeave}>Leave unchanged</button>
        </div>
      </section>}

      {change.stage === 'stopped' && <section className="card">
        <h2>Left unchanged.</h2>
        <p>Preparation stopped. No new actions will run. Stopping cannot retract any external work already completed.</p>
        <button onClick={props.onPrepare} disabled={busy}>Prepare a new plan</button>
      </section>}

      {editing && props.editForm}

      {change.stage === 'proposal_ready' && !editing && proposal && digest && <>
        <CareDigest digest={digest} members={props.circle ?? []} approvedArtifact={props.approvedArtifact ?? null} onEdit={props.onEditDigest} busy={busy} owner={owner} />
        {(stale || expired) && <div className="alert error" role="alert">
          <strong>{stale ? 'The digest changed. Read it again.' : 'This digest has expired.'}</strong>
          <p>{stale ? `The current version is ${change.version}. The earlier approval cannot be used.` : `Expired ${time(proposal.expires_at, today.timezone)} ${today.timezone}.`}</p>
        </div>}
        {!owner && <p className="attention">Sign in as the owner before approving. A notification or an open link is not consent.</p>}
        {!validBundle && !stale && !expired && <p className="attention">This digest is incomplete — it has no audience or no lines. Nothing can be sent.</p>}
        <div className="decision-bar">
          <div className="button-row action-group">
            <button className="primary approve" disabled={busy || offline || stale || expired || !owner || !validBundle} onClick={props.onApprove}>Approve and send</button>
            <button disabled={busy} onClick={props.onLeave}>Don’t send today</button>
          </div>
          <small>Approving sends exactly the words above, to exactly the people above, and nothing else. At most one digest goes out a day.</small>
        </div>
      </>}

      {change.stage === 'proposal_ready' && !editing && proposal && !digest && calendar?.kind === 'calendar_update' && mail?.kind === 'email_reply' && <>
        <section className="card proposed-change">
          <h2>Proposed change</h2>
          <p className="compact-diff tabular">
            <span className="cd-old">{time(calendar.diff.start[0], today.timezone)} · {calendar.diff.location[0]}</span>
            <span className="cd-arrow" aria-hidden="true">→</span>
            <strong className="cd-new">{time(calendar.diff.start[1], today.timezone)}–{time(calendar.diff.end[1], today.timezone)} · {calendar.diff.location[1]}</strong>
          </p>
          <p className="secondary">{calendar.notifies_attendees ? `Attendees will get an updated invitation, and ${who} will get the reply below.` : 'This update will not notify attendees.'}</p>
          <div className="email reply-preview">
            <p><span>To</span> <strong>{mail.to.join(', ')}</strong></p>
            <p><span>Subject</span> {mail.subject}</p>
            <div className="email-body">{mail.body}</div>
          </div>

          {proposal.rejected.length > 0 && <div className="left-out">
            <h3>Left out of this plan</h3>
            <ul>{proposal.rejected.map((option, index) => <li key={`${option.name}-${index}`}>
              <strong>{option.name}</strong>
              <span>{option.reason}</span>
              {option.by && <span className="by-skill">Removed by your rule · {option.by}</span>}
            </li>)}</ul>
          </div>}
        </section>

        {(stale || expired) && <div className="alert error" role="alert">
          <strong>{stale ? 'The plan changed. Review it again.' : 'This proposal has expired.'}</strong>
          <p>{stale
            ? `The current plan is version ${change.version}. The earlier approval cannot be used. ${change.blocked_reason ?? ''}`
            : `Expired ${time(proposal.expires_at, today.timezone)} ${today.timezone}.`}</p>
          <button onClick={props.onPrepare}>Prepare a fresh proposal</button>
        </div>}
        {!owner && <p className="attention">Sign in as the owner before approving. A notification or an open link is not consent.</p>}
        {!validBundle && !stale && !expired && <p className="attention">The action bundle is incomplete. Refresh the plan before approving.</p>}

        <div className="decision-bar">
          <div className="button-row action-group">
            {/* One action approves exactly the two items shown above — no second "review" click that
                repeats a decision the owner already made by reading this far. */}
            <button className="primary approve" disabled={busy || offline || stale || expired || !owner || !validBundle} onClick={props.onApprove}>Approve this change</button>
            <button onClick={props.onStartEdit}>Edit</button>
            <button disabled={busy} onClick={props.onLeave}>Leave unchanged</button>
          </div>
          <small>Approving runs exactly the calendar update and the reply shown above — nothing else.</small>
        </div>
      </>}

      {(finished || executing) && <section className="card outcome" aria-live="polite">
        {/* "Simulated — no external changes were made" once, at the top of the receipt — never
            repeated line by line as "would" versus "did". Everything below it is the same past-tense
            receipt live mode would show. */}
        {sample && <span className="sim-badge">Simulated — no external changes were made</span>}
        <h2>{change.executed_by === 'limit' && change.stage === 'done' ? 'Done within your limits' : finished ? change.stage === 'done' ? 'Your afternoon still fits.' : 'Part of this plan needs you.' : 'Action status'}</h2>
        {/* The concise receipt, read before the itemized detail below it: what changed, who was told,
            and that the protected boundary held regardless. */}
        {receiptLine && <p className="receipt-line">{receiptLine}</p>}
        {outcome?.effects.map(effect => <div className={`effect ${effect.state}`} key={effect.kind}>
          <span className="effect-symbol" aria-hidden="true">{effect.state === 'done' ? '✓' : effect.state === 'failed' ? '!' : '◷'}</span>
          <div>
            <strong>{effectLabel(effect.kind, effect.state)}</strong>
            <p>{effect.detail}</p>
            {/* Only a real absolute http(s) URL gets a link. This one is a "provider record" — the
                most trusted-sounding link on the page and the one the owner is most likely to click,
                which is exactly why it is checked like every other. */}
            {providerHref(effect.provider_url) && <a href={providerHref(effect.provider_url)!} target="_blank" rel="noreferrer">Open {effect.kind === 'calendar_update' ? 'calendar event' : 'sent email'}</a>}
          </div>
        </div>)}
        <OverreachList events={outcome?.overreach} />
        {change.stage === 'partial' && <p className="attention">No automatic resend. Check the recorded result before starting a new action. Completed work cannot be undone here.</p>}
        {/* A real next action, not a demo-oriented one: back to the record this receipt just joined. */}
        {finished && <div className="button-row outcome-cta"><button className="primary" onClick={props.onBack}>Back to Today</button></div>}
        {props.afterActions}
      </section>}

      {proposal && <details className="day-disclosure"><summary>See the full day</summary><Day today={today} change={change} outcome={outcome} /></details>}

      {/* Teaching is about which offered TIMES fit: a taught rule narrows a scheduling plan and
          can do nothing else. It is therefore not offered on a digest (no times were offered) or
          on a refused change (there is no plan for a rule to narrow, and the answer to an attack
          is not a preference). */}
      {props.learning && !digest && !refused && <details className="teach-fold"><summary>Teach it once — remember this preference</summary>{props.learning}</details>}
    </div>

    <details className="how-checked">
      <summary>
        <span className="how-checked-title">How this was checked</span>
        {/* The sub-line promises a decision log. When there is none, saying so here is the
            difference between a collapsed summary that is accurate and one that implies a check
            happened behind it. */}
        <span className="how-checked-sub">{checksUnread
          ? `The email and the ${sample ? 'sample proposal' : "model's suggestion"} — the decision log did not load, so nothing below claims a check passed.`
          : `The email, the ${sample ? 'sample proposal' : "model's suggestion"}, and what trusted code checked — with the decision log.`}</span>
      </summary>

      <div className="how-tabs">
        {/* A CSS-only radio-tabs pattern: the radios and labels already carry correct, complete
            native semantics, so this wrapper is a `group` around them rather than a `tablist` —
            `tablist` promises `role="tab"` children, which these are not. */}
        <div className="columns-mobile-tabs" role="group" aria-label="How this change was handled">
          <input type="radio" name={`how-tab-${change.id}`} id={`tab-arrived-${change.id}`} className="tab-radio" defaultChecked />
          <label className="tab-label" htmlFor={`tab-arrived-${change.id}`}>What arrived</label>
          <input type="radio" name={`how-tab-${change.id}`} id={`tab-model-${change.id}`} className="tab-radio" />
          <label className="tab-label" htmlFor={`tab-model-${change.id}`}>{col2Title}</label>
          <input type="radio" name={`how-tab-${change.id}`} id={`tab-allowed-${change.id}`} className="tab-radio" />
          <label className="tab-label" htmlFor={`tab-allowed-${change.id}`}>{checksUnread ? 'The decision log did not load' : 'What trusted code checked'}</label>
        </div>

        <div className="columns" aria-label="How this change was handled">
          <section className="column untrusted panel-arrived" aria-labelledby="col-arrived">
            <h2 id="col-arrived" className="column-title"><span className="column-step">1</span>What arrived</h2>
            <span className="provenance-chip">{digest ? 'Your own records · trusted' : 'Email · untrusted'}</span>
            {digest
              // A digest arrives from nowhere: it is composed from records this machine already
              // holds. Rendering an empty untrusted quote over it would claim an inbound message
              // that does not exist, on the one column whose whole job is provenance.
              ? <p className="column-note">Nothing arrived. This digest was composed from {digest.fragments.filter(f => f.record_id).length} record(s) on this machine — the calendar events, the open change and the message each line names.</p>
              : <EmailInput excerpt={change.request_excerpt} />}
            <p className="column-note">{digest ? 'No sender, because nobody sent it.' : summary ? `From ${summary.from_name} · ${summary.from_address}` : 'From the selected thread'}</p>
            {threadHref
              ? <a href={threadHref} target="_blank" rel="noreferrer">Open original thread</a>
              : <small>{sample ? 'Selected sample email · the original thread is synthetic' : 'Original thread link unavailable'}</small>}
          </section>

          <span className="seam" aria-hidden="true" />

          <section className="column untrusted panel-model" aria-labelledby="col-model">
            <h2 id="col-model" className="column-title"><span className="column-step">2</span>{col2Title}</h2>
            <span className="provenance-chip">{sample ? 'Sample data · untrusted' : 'Model · untrusted'}</span>
            {proposal
              ? <>
                <p className="proposed-headline">{time(proposal.start, today.timezone)}–{time(proposal.end, today.timezone)} at {proposal.venue.name}</p>
                <p>{proposal.reason_sentence}</p>
                <Source evidence={proposal.venue.evidence} />
                <p className="column-note">{proposal.venue.note}</p>
                {proposal.rejected.length > 0 && <p className="column-note">What this plan left out is shown above, in “Left out of this plan.”</p>}
              </>
              : <p className="secondary">Nothing has been proposed for this change yet.</p>}
            {/* "What the model was shown" reading right above "No model was called for this change"
                was the contradiction a judge saw — even though the panel goes on to explain itself,
                the title alone looked wrong before anyone read the body. A title that matches what
                this particular change actually did removes the apparent contradiction at a glance. */}
            {/* Three answers, not two. "What the model was shown" is a claim that a receipt was
                read; when the receipt is forbidden or absent the title has to say that instead of
                naming a thing it does not have. */}
            <details className="receipt-disclosure"><summary>{props.modelViewState === 'forbidden'
              ? 'The model receipt is not readable from here'
              : props.modelViewState === 'unavailable' || !props.modelView
                ? 'No model receipt was returned'
                : props.modelView.calls.length === 0 ? 'What stayed private' : 'What the model was shown'}</summary>
              <ModelReceipt view={props.modelView} state={props.modelViewState} />
            </details>
            <p className="column-note">A suggestion is not permission. Nothing in this column can cause an effect.</p>
          </section>

          <span className="seam" aria-hidden="true" />

          <section className="column trusted panel-allowed" aria-labelledby="col-allowed">
            <h2 id="col-allowed" className="column-title"><span className="column-step">3</span>{checksUnread ? 'The decision log did not load' : 'What trusted code checked'}</h2>
            <span className="provenance-chip trusted-chip">Trusted code · the only authority</span>
            {checksUnread
              ? <p className="column-note">The checks this change made could not be read, so this column reports none of them. Nothing here is an assumption that a check passed. The two actions above are still exactly what an approval would run — what is missing is the record of what was checked before they could.</p>
              : proposal && <p className="column-note">The exact calendar update and reply are shown above, in “Proposed change.” This is what trusted code checked before either could run.</p>}

            {/* What it refused comes AFTER what it allowed. The column's question is "what did trusted
                code allow", and leading with a red panel answers a different one. */}
            {refusal && <RefusalCard decision={refusal} completed={executing || finished} />}

            {clauseRows.length > 0 && <section className="clause-verdicts">
              <h3>Your limit clauses</h3>
              <ul>{clauseRows.map(row => {
                const clause = clauses.find(c => c.text === row.rule)!;
                return <li key={`${clause.id}-${row.seq}`} data-verdict={row.verdict}>
                  <span className={`verdict ${row.verdict}`}>{row.verdict}</span>
                  <span><strong>Clause {clause.id}</strong> {clause.text}</span>
                </li>;
              })}</ul>
              {handback.length > 0 && <p className="attention" role="status">Nothing ran under your limits. {handback.length === 1 ? 'One clause' : `${handback.length} clauses`} did not pass, so this came back to you.</p>}
            </section>}

            <DecisionLog key={change.id} clauses={clauses} decisions={records} unavailable={decisions?.unavailable} />
          </section>
        </div>
      </div>

      {proposal && <div className="why-row">
        <p className="secondary">Every time, venue and recipient above came from somewhere. This is where from.</p>
        <button className="text-button" onClick={props.onWhy}>Why this plan?</button>
      </div>}
    </details>
  </div>;
}
