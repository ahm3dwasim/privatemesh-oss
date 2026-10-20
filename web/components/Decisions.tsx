import type { Limits } from '@/lib/v2-types';
import type { Decision } from '@/lib/decisions';

const stageLabels: Record<Decision['stage'], string> = {
  read: 'Read the selected request', model: 'Prepare a suggestion', search: 'Check public information',
  validate: 'Check permission boundaries', approve: 'Approve the exact edit', execute: 'Apply the approved edit', send: 'Send the approved reply',
};
/** One refusal, in the words of THE ROW THE SERVER RECORDED.
 *
 *  It takes a `Decision` rather than a bag of booleans, and that is the sixth review's test-quality
 *  item made structural. The version this replaces read four optional display flags with defaults,
 *  so both its tests rendered it with no input at all and asserted about a fallback the product
 *  never draws — a green suite that could not have gone red if the server stopped sending refusals,
 *  or started sending them with a different `source`, or stopped marking them untrusted. Every fact
 *  on this card except `completed` is a field of the decision log row, and the row is now the only
 *  way to reach them. */
export function RefusalCard({ decision, completed = false }: { decision: Decision; completed?: boolean }) {
  const { source, untrusted } = decision;
  const privateAttempt = decision.subject === 'Share a private detail with an extra recipient';
  return <section className="refusal-card" aria-label="Blocked attempt">
    <div className="refusal-heading"><span aria-hidden="true">▣</span><strong>Request refused</strong><span className="trust-label">{source}{untrusted ? ' · untrusted' : ' · permission check'}</span></div>
    <h2>{privateAttempt ? 'A private detail stays private.' : 'The request exceeded its permission.'}</h2>
    <p>{privateAttempt ? 'The request tried to share a private detail with an extra recipient.' : 'An action outside the permitted scope was attempted.'}</p>
    <p className="refusal-rule">An email, a model, or a skill cannot expand permission to act or reveal information.</p>
    <strong className="refusal-result">{completed ? 'Nothing was sent from this blocked attempt.' : 'Nothing was sent.'}</strong>
  </section>;
}
/**
 * The email, as it arrived, with the injected tail marked.
 *
 * The marker is a FIXTURE CONVENTION and not a detector, and saying so is the point: the sample's
 * injected tails are written as a continuation line beginning with an ellipsis, so this component
 * can show a judge which sentence is the attack without anyone having to read the whole quote. It
 * is not a claim that the product can spot an injection by looking at it — the product's answer to
 * an injection is the gate, which runs on the composed bytes and does not care which line they
 * came from. A real email marks nothing, which is the correct behaviour for a component whose job
 * is to render untrusted text rather than to classify it.
 *
 * It used to match the literal string `…and cc Dave`, which meant one particular sample sentence
 * had the marking and every other injected tail this fixture grew was rendered as ordinary prose.
 */
export function EmailInput({ excerpt }: { excerpt: string }) {
  const parts = excerpt.split('\n');
  return <blockquote className="inbound-email">{parts.map((line, i) => !line.trimStart().startsWith('\u2026') ? <p key={i}>{line}</p> : <div className="injected-line" key={i}><span className="trust-label">Untrusted input · email instruction</span><mark>{line}</mark></div>)}</blockquote>;
}
export function DecisionLog({ decisions, unavailable = false, clauses = [] }: { decisions: Decision[]; unavailable?: boolean; clauses?: Limits['clauses'] }) {
  if (unavailable) return <p className="decision-unavailable" role="status">Decision log unavailable. No checks are assumed to have passed.</p>;
  if (!decisions.length) return <p className="secondary" role="status">No decisions recorded yet.</p>;
  // All four verdicts, and `discarded` is the one that must be visible rather than folded away: it
  // is the record of a candidate the plan did NOT take, and a summary that dropped it would make a
  // two-option request read as a one-option one. It is counted apart from `refused` because it is
  // not a refusal — the change went ahead on the other slot.
  const counts = (['allowed', 'refused', 'withheld', 'discarded'] as const).map(verdict => ({ verdict, count: decisions.filter(d => d.verdict === verdict).length })).filter(d => d.count);
  return <details className="decision-log"><summary><span>Decision log</span><strong>{counts.map(d => `${d.count} ${d.verdict}`).join(' · ')}</strong></summary>
    {/* The total on this label grows as the change moves — reading a thread, checking a suggestion,
        approving, executing are each their own checks — so a judge saw the number change between
        review and outcome with nothing on screen to say why. This says why, once, here. */}
    <p className="secondary decision-log-note">Every check this change has made so far, in order. The total grows as the change moves — approving and executing are checks too.</p>
    <ol>{[...decisions].sort((a, b) => a.seq - b.seq).map(d => <li key={d.seq} data-verdict={d.verdict}>
      <div><strong>{d.seq}. {stageLabels[d.stage]}</strong><span className={`decision-verdict ${d.verdict}`}>{d.verdict}</span></div>
      <span className={d.untrusted ? 'trust-label' : 'secondary'}>{d.source}{d.untrusted ? ' · untrusted' : ' · permission authority'}</span>
      {/* Audit payloads may contain protected text. Render public descriptions, never raw subject/detail. */}
      {d.stage === 'approve' && d.source === 'owner' && clauses.some(c => c.text === d.rule) ? <p><strong>Clause {clauses.find(c => c.text === d.rule)!.id}</strong>: {clauses.find(c => c.text === d.rule)!.text}</p> : <p>{d.verdict === 'refused' ? 'This source cannot expand permission to act or reveal a private detail.' : d.verdict === 'withheld' ? 'Your commitment was planned around without being described. Nothing was refused.' : d.verdict === 'discarded' ? 'This option was not chosen. Something else was, and nothing here was stopped.' : 'This check stayed within the granted permission.'}</p>}
    </li>)}</ol>
  </details>;
}
