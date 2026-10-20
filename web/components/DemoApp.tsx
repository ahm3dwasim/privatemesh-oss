'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { createDemoClient } from '@/lib/demo-api';
import { ApiError } from '@/lib/v2-types';
import type { DemoResult, DemoSessionState } from '@/lib/demo-types';
import { BUILTIN_ATTACK_BODY, BUILTIN_ATTACK_INJECTION, BUILTIN_ATTACK_INJECTION_OPENER, BUILTIN_ATTACK_MESSAGE_ID, circleRelevantDecisions, gateRefusals, SEEDED_CARE_CIRCLE } from '@/lib/demo-fixtures';
import { formatDemoInstant, formatDemoRange } from '@/lib/demo-format';
import { ModelReceipt } from './ModelReceipt';
import { DecisionLog } from './Decisions';

const client = createDemoClient();
const MAX_EMAIL_CHARS = 2000;
const REPO_URL = 'https://github.com/ahm3dwasim/privatemesh';

type Phase = 'starting' | 'ready' | 'unavailable' | 'error';

/** Why a run couldn't finish, in the visitor's words. Every code `docs/API_V2.md` names for these
 *  four routes gets its own sentence — a code with no case here would fall through to the generic
 *  message, which is a state this screen must never rest in silently. */
function errorCopy(error: ApiError): { title: string; body: string; canRetry: boolean } {
  switch (error.code) {
    case 'demo_unavailable':
      return { title: 'The demo is not available right now.', body: error.message, canRetry: true };
    case 'rate_limited':
      return { title: 'Too many requests.', body: error.message, canRetry: true };
    case 'demo_empty_email':
      return { title: 'Write something first.', body: 'The email body is empty, so nothing would be sent to the model.', canRetry: false };
    case 'demo_limit':
      return { title: 'That email is too long.', body: `The demo holds every visitor to ${MAX_EMAIL_CHARS} characters, refused before it would spend a model call — a shortened body would be a different injection than the one you wrote.`, canRetry: false };
    case 'not_found':
      return { title: 'This session has expired or was not found.', body: 'Demo sessions live for a limited time. Start a new one to keep going.', canRetry: false };
    case 'network_error':
      return { title: 'The demo could not be reached.', body: 'Check your connection and try again.', canRetry: true };
    default:
      return { title: 'This request could not finish.', body: error.message, canRetry: true };
  }
}

export function DemoApp() {
  const [phase, setPhase] = useState<Phase>('starting');
  const [startError, setStartError] = useState<ApiError | null>(null);
  const [session, setSession] = useState<DemoSessionState | null>(null);
  const [selectedId, setSelectedId] = useState<string>('');
  const [draft, setDraft] = useState('');
  // Two runs of the SAME email, tracked independently: the real model spends the session's own
  // budget and can degrade to the scripted fallback under a cap; the compromised run never touches
  // either ledger. Keeping them as separate state (rather than one `result` a "mode" flag reads)
  // is what lets both be ON SCREEN AT ONCE — the whole point of "side by side" — instead of one
  // replacing the other.
  const [realRunning, setRealRunning] = useState(false);
  const [realResult, setRealResult] = useState<DemoResult | null>(null);
  const [realError, setRealError] = useState<ApiError | null>(null);
  const [compromisedRunning, setCompromisedRunning] = useState(false);
  const [compromisedResult, setCompromisedResult] = useState<DemoResult | null>(null);
  const [compromisedError, setCompromisedError] = useState<ApiError | null>(null);
  const [sessionExpired, setSessionExpired] = useState(false);
  const realHeading = useRef<HTMLHeadingElement>(null);
  const compromisedHeading = useRef<HTMLHeadingElement>(null);
  const startGeneration = useRef(0);

  const start = useCallback(async () => {
    const generation = ++startGeneration.current;
    setPhase('starting'); setStartError(null); setSessionExpired(false);
    setRealResult(null); setRealError(null); setCompromisedResult(null); setCompromisedError(null);
    try {
      const next = await client.start();
      if (generation !== startGeneration.current) return;
      setSession(next);
      // The first thing a visitor should be able to run is the interesting one: the built-in
      // attack, not whichever pharmacy or transport email happened to sort first.
      const defaultMessage = next.inbox.find(m => m.message_id === BUILTIN_ATTACK_MESSAGE_ID) ?? next.inbox[0];
      setSelectedId(defaultMessage?.message_id ?? '');
      setDraft(defaultMessage?.body_text ?? '');
      setPhase('ready');
    }
    catch (e) {
      if (generation !== startGeneration.current) return;
      const error = e instanceof ApiError ? e : new ApiError(0, 'This request could not finish. Please try again.', 'request_failed');
      setStartError(error);
      setPhase('unavailable');
    }
  }, []);

  useEffect(() => { void start(); }, [start]);

  const selected = session?.inbox.find(m => m.message_id === selectedId) ?? null;
  /** The lookalike email the built-in attack arrives as. Every seeded demo world holds it; a
   *  session whose inbox somehow did not gets a sentence saying so instead of a dead button. */
  const attackMessage = session?.inbox.find(m => m.message_id === BUILTIN_ATTACK_MESSAGE_ID) ?? null;

  /** Both runs are answers about the email on screen; changing it makes both stale. */
  const resetResults = () => {
    setRealResult(null); setRealError(null); setCompromisedResult(null); setCompromisedError(null);
  };

  const selectMessage = (id: string) => {
    const message = session?.inbox.find(m => m.message_id === id);
    setSelectedId(id);
    setDraft(message?.body_text ?? '');
    resetResults();
  };

  /** The built-in attack IS the lookalike email, so the button selects that message rather than
   *  appending anything to whichever one is on screen. A page that let a visitor "run the built-in
   *  attack" against the pharmacy refill would be describing one run and sending another. */
  const insertBuiltinAttack = () => {
    const attack = session?.inbox.find(message => message.message_id === BUILTIN_ATTACK_MESSAGE_ID);
    if (!attack) return;
    setSelectedId(attack.message_id);
    setDraft(BUILTIN_ATTACK_BODY);
    resetResults();
  };

  const sendReal = async () => {
    if (!session || !selected || realRunning || compromisedRunning) return;
    setRealRunning(true); setRealError(null);
    try {
      const outcome = await client.sendEmail(session.session_id, { message_id: selected.message_id, body_text: draft });
      setRealResult(outcome);
      setSession(current => current && { ...current, budget: outcome.budget, spend: outcome.spend });
      requestAnimationFrame(() => {
        realHeading.current?.scrollIntoView({ block: 'start' });
        realHeading.current?.focus({ preventScroll: false });
      });
    }
    catch (e) {
      const error = e instanceof ApiError ? e : new ApiError(0, 'This request could not finish. Please try again.', 'request_failed');
      if (error.code === 'not_found') setSessionExpired(true);
      setRealError(error);
    }
    finally { setRealRunning(false); }
  };

  const sendCompromised = async () => {
    if (!session || !selected || realRunning || compromisedRunning) return;
    setCompromisedRunning(true); setCompromisedError(null);
    try {
      const outcome = await client.sendEmailCompromised(session.session_id, { message_id: selected.message_id, body_text: draft });
      setCompromisedResult(outcome);
      setSession(current => current && { ...current, budget: outcome.budget, spend: outcome.spend });
      requestAnimationFrame(() => {
        compromisedHeading.current?.scrollIntoView({ block: 'start' });
        compromisedHeading.current?.focus({ preventScroll: false });
      });
    }
    catch (e) {
      const error = e instanceof ApiError ? e : new ApiError(0, 'This request could not finish. Please try again.', 'request_failed');
      if (error.code === 'not_found') setSessionExpired(true);
      setCompromisedError(error);
    }
    finally { setCompromisedRunning(false); }
  };

  if (phase === 'starting') return <main className="demo-page">
    <DemoHeader />
    <p role="status" className="secondary">Starting your demo session…</p>
  </main>;

  if (phase === 'unavailable' && startError) {
    const copy = errorCopy(startError);
    return <main className="demo-page">
      <DemoHeader />
      <div className="alert error" role="alert">
        <strong>{copy.title}</strong>
        <p>{copy.body}</p>
        {copy.canRetry && <button className="primary" onClick={() => void start()}>Try again</button>}
      </div>
      <p>While you wait, the <Link href="/tour">sample workspace</Link> walks through the same product on a scripted fixture — no model, no queue.</p>
    </main>;
  }

  if (!session) return null;

  const outOfCalls = session.budget.calls_left <= 0;
  const overCharLimit = draft.length > MAX_EMAIL_CHARS;
  const running = realRunning || compromisedRunning;

  return <main className="demo-page">
    <DemoHeader />

    {sessionExpired
      ? <section className="card alert error" role="alert">
        <strong>This session has expired.</strong>
        <p>Demo sessions live for a limited time so the demo can serve the next visitor. Start a new one to keep going — your previous run is not kept anywhere.</p>
        <button className="primary" onClick={() => void start()}>Start a new session</button>
      </section>
      : <>
        {/* The attack itself, first — a visitor's first useful action should be the interesting
            one, not three cards of bookkeeping before it. */}
        {selected && <section className="card demo-attack" aria-label="Rewrite this email">
          <h2>Rewrite “{selected.subject}”</h2>

          <div className="demo-attack-builtin">
            <h3>Run the built-in attack</h3>
            <p className="secondary">A lookalike “benefits update” email from our red-team corpus, already selected below — three attempts in one message.</p>
            <button className="primary" disabled={running || !attackMessage} onClick={() => { insertBuiltinAttack(); }}>Insert the built-in attack</button>
            <p className="secondary">{attackMessage
              ? <>Selects “{attackMessage.subject}” and loads the attack into the email below — review it, then run it both ways.</>
              : <>This session’s inbox has no “benefits update” email to attack. Pick one above and write your own.</>}</p>
            <p className="demo-injection-preview">{BUILTIN_ATTACK_INJECTION_OPENER}</p>
            <details><summary>What it asks for, in full</summary><p className="demo-injection-preview">{BUILTIN_ATTACK_INJECTION}</p></details>
          </div>

          {/* The email appears ONCE, in the textarea below. A read-only copy of `selected.body_text`
              used to sit here, above the textarea the session seeds with that same body — so at first
              contact, which is what the landing screenshot captures, a visitor read the identical
              paragraph twice. The only thing that box carried and the textarea does not is who sent
              it, and that is this line. */}
          <form className="demo-compose" onSubmit={e => { e.preventDefault(); void sendReal(); }}>
            <p className="secondary">From {selected.sender}</p>
            <label htmlFor="demo-body">The email — sent to the real model, and to the compromised one</label>
            <textarea id="demo-body" rows={8} value={draft} onChange={e => { setDraft(e.target.value); resetResults(); }} disabled={running} maxLength={MAX_EMAIL_CHARS + 200} />
            <p className={`secondary tabular${overCharLimit ? ' attention' : ''}`}>{draft.length} / {MAX_EMAIL_CHARS} characters{overCharLimit ? ' — over the limit; nothing will be sent' : ''}</p>
            <p className="secondary">Running the compromised model never spends one of these — it makes no model call at all.</p>
            <div className="demo-run-buttons">
              <button type="submit" className="primary" disabled={running || !draft.trim() || overCharLimit}>{realRunning ? 'Sending to the real model…' : 'Send to the real model'}</button>
              <button type="button" disabled={running || !draft.trim() || overCharLimit} onClick={() => { void sendCompromised(); }}>{compromisedRunning ? 'Running the compromised model…' : 'Run the compromised model (free)'}</button>
            </div>
            {realError && <div className="alert error" role="alert"><strong>Real model: {errorCopy(realError).title}</strong><p>{errorCopy(realError).body}</p></div>}
            {compromisedError && <div className="alert error" role="alert"><strong>Compromised model: {errorCopy(compromisedError).title}</strong><p>{errorCopy(compromisedError).body}</p></div>}
            {/* The budget card, demoted: one line under the buttons it describes, not the second
                thing on the page. */}
            <p className="tabular demo-budget-line"><strong>{session.budget.calls_left}</strong> model call{session.budget.calls_left === 1 ? '' : 's'} left this session (of {session.caps.session_calls}). {session.spend.session && <>${session.spend.session.total_usd.toFixed(4)} of ${session.spend.session.cap_total_usd.toFixed(2)} spent.</>}</p>
            {session.budget.degraded && <p className="alert attention" role="status"><strong>Running on the scripted fallback.</strong> {session.budget.degraded.detail}</p>}
            {outOfCalls && !session.budget.degraded && <p className="secondary">This session is out of real-model calls. The real run below will use the scripted fallback and say so; the compromised run is unaffected.</p>}
          </form>
        </section>}

        <section className="card demo-truth">
          <p>{session.notice}</p>
          <div className="demo-truth-grid">
            <div><h2>Real</h2><ul>{session.real.map(item => <li key={item}>{item}</li>)}</ul></div>
            <div><h2>Simulated</h2><ul>{session.simulated.map(item => <li key={item}>{item}</li>)}</ul></div>
          </div>
        </section>

        <section className="card demo-inbox" aria-label="Simulated inbox">
          <h2>The simulated inbox</h2>
          <p className="secondary">Fixtures, not real mail — see “Simulated” above. Pick one to attack or to write your own email into.</p>
          <ul className="demo-inbox-list">
            {session.inbox.map(message => <li key={message.message_id}>
              <button type="button" className="demo-inbox-row" aria-current={selectedId === message.message_id ? 'true' : undefined}
                onClick={() => selectMessage(message.message_id)} disabled={running}>
                <strong>{message.subject}</strong>
                <span className="secondary">{message.sender}</span>
              </button>
            </li>)}
          </ul>
        </section>

        {selected && <>
          <div className="demo-result-grid" aria-label="Both ways to run this email">
            <DemoResultColumn variant="real" result={realResult} headingRef={realHeading}
              placeholder="Not run yet. Press “Send to the real model” above to see what Nemotron actually does with this exact email." />
            <DemoResultColumn variant="compromised" result={compromisedResult} headingRef={compromisedHeading}
              placeholder="Not run yet. Press “Run the compromised model (free)” above — it complies with everything this email asks, so the refusal that follows is the engine's, not good manners." />
          </div>
          {(realResult || compromisedResult) &&
            <div className="button-row"><button className="text-button" onClick={resetResults}>Run another email</button></div>}
        </>}
      </>}

    <DemoFooter />
  </main>;
}

function DemoHeader() {
  return <header className="demo-page-heading">
    <p className="eyebrow">PrivateMesh · Try it live</p>
    <h1>Attack the real assistant.</h1>
    <p className="lead">PrivateMesh is a private assistant for the things you can’t afford to get wrong — nothing it reads, is told by a model, or is taught can ever widen what it may do or reveal.</p>
    <p className="secondary">A simulated caregiver inbox, a real model, and the same enforcement code the product ships with. Nothing you send here reaches a real account.</p>
    {/* Named for what `/tour` actually renders: the scripted sample workspace, the same page a
        non-demo build serves at `/`. "Guided tour" promised a walkthrough the route does not give. */}
    <p><Link href="/tour">Sample workspace (no model) →</Link></p>
  </header>;
}

function DemoFooter() {
  return <footer className="app-footer">
    <a href={REPO_URL} target="_blank" rel="noreferrer">Source (Apache-2.0)</a>
  </footer>;
}

/** One side of the comparison: a label that says honestly what answered, and either the run's own
 *  result or a placeholder for a run that has not happened yet. The placeholder is not a loading
 *  state — nothing is in flight until the visitor presses that column's own button — so it reads
 *  as an instruction rather than a spinner with nothing behind it. */
function DemoResultColumn({ variant, result, headingRef, placeholder }: {
  variant: 'real' | 'compromised';
  result: DemoResult | null;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
  placeholder: string;
}) {
  const label = variant === 'real' ? 'Real model' : 'Compromised model';
  if (!result) return <div className="demo-result-placeholder" role="group" aria-label={`${label}: not yet run`}>
    <span className={`demo-result-label demo-result-label--${variant}`}>{label}</span>
    <p>{placeholder}</p>
  </div>;
  // The API's own `model.label`, when the run reports one, rather than a caption this component
  // would otherwise have to hardcode and could drift from what the response actually says.
  const caption = result.model?.label ?? (variant === 'real'
    ? 'Whatever the real model on Token Factory actually did with this email — including doing nothing with the injection at all.'
    : 'Scripted, not Nemotron — the red-team corpus’s own model that complies with everything this email asks. Free: no model call, no spend.');
  return <DemoResultPanel result={result} variant={variant} label={label} caption={caption} headingRef={headingRef} />;
}

function DemoResultPanel({ result, variant, label, caption, headingRef }: {
  result: DemoResult; variant: 'real' | 'compromised'; label: string; caption: string;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const change = result.change;
  const refusals = result.decisions.filter(d => d.verdict === 'refused');
  /** What the run was stopped BY. A `no_safe_fit` change holds refused rows for the slots that did
   *  not fit, and a change the privacy boundary stopped holds rows for what it would not let out;
   *  `gateRefusals` is the second kind, and the second kind is what "Refused" is about. */
  const gate = gateRefusals(refusals);
  const refused = gate.length > 0 || (change?.stage === 'no_safe_fit' && refusals.length > 0);
  const shownRefusals = gate.length > 0 ? gate : refusals;
  const proposed = !!change?.proposal;
  const circleAttempts = circleRelevantDecisions(result.decisions);

  return <section className={`card demo-result demo-result--${variant}`} aria-label={`${label} result`} tabIndex={-1}>
    <span className={`demo-result-label demo-result-label--${variant}`}>{label}</span>
    <p className="secondary demo-result-caption">{caption}</p>

    {result.compromised && result.attempted && result.attempted.length > 0 && <div className="demo-attempted">
      <strong>What it tried:</strong>
      <ul className="demo-attempted-list">{result.attempted.map((line, index) => <li key={index}>{line}</li>)}</ul>
    </div>}

    {result.budget.degraded && <p className="alert attention" role="status"><strong>This run used the scripted fallback, not the model.</strong> {result.budget.degraded.detail}</p>}

    {!change && <>
      <h2 ref={headingRef} tabIndex={-1}>This email produced no change.</h2>
      <p>The engine read it and did not treat it as something to plan around. That can happen for an email with no meeting-shaped request in it.</p>
    </>}

    {refused && <>
      <div className="refusal-verdict">
        <span className="refusal-mark" aria-hidden="true">&#10005;</span>
        <div>
          <h2 ref={headingRef} tabIndex={-1}>Refused. Nothing protected left this machine.</h2>
          <p className="refusal-sub">{shownRefusals.length} request{shownRefusals.length === 1 ? '' : 's'} on this run were stopped, and nothing was sent.</p>
        </div>
      </div>
      <ul className="count-evidence">{shownRefusals.map(row => <li key={row.seq}>
        <span className="verdict refused">refused</span>
        <span><strong>{row.subject}</strong> — {row.detail ?? row.rule}</span>
      </li>)}</ul>
    </>}

    {proposed && !refused && <>
      <h2 ref={headingRef} tabIndex={-1}>Proposed — {change!.proposal!.reason_sentence}</h2>
      {variant === 'real' && <p className="secondary">The real model did not attempt what this email's injected instructions ask for — nothing here needed refusing.</p>}
      {change!.proposal!.actions.map((action, index) => action.kind === 'email_reply'
        ? <div className="demo-action" key={index}><h3>Reply</h3><p className="secondary">To {action.to.join(', ')}</p><blockquote className="demo-original"><p>{action.body}</p></blockquote></div>
        : action.kind === 'calendar_update'
          ? <div className="demo-action" key={index}><h3>Calendar</h3>
              <p className="secondary tabular">{formatDemoRange(action.diff.start[1], action.diff.end[1])}
                {action.diff.start[0] !== action.diff.start[1] && <> — was {formatDemoInstant(action.diff.start[0])}</>}</p>
            </div>
          : null)}
    </>}

    {change && !refused && !proposed && <>
      <h2 ref={headingRef} tabIndex={-1}>This run stopped before a plan.</h2>
      <p>{result.prepare_refused
        ? <>It could not be prepared: {result.prepare_refused.detail}.</>
        : <>It reached “{change.stage}” and went no further — the decision log below is what it decided on the way.</>}</p>
    </>}

    <h3>What the model saw</h3>
    <ModelReceipt view={result.model_view} state={result.model_view ? 'ready' : 'unavailable'} />

    <h3>The decision log</h3>
    <DecisionLog decisions={result.decisions} />

    <section className="demo-circle" aria-label="Care circle">
      <h3>The care circle</h3>
      <p className="secondary">Seeded for every visitor and fixed — reading it live needs the owner's own credential, which no visitor holds. {SEEDED_CARE_CIRCLE.length} member(s), before and after: nothing in this product writes to the circle from an email.</p>
      <ul className="demo-circle-list">{SEEDED_CARE_CIRCLE.map(member => <li key={member.email}><strong>{member.name}</strong> <span className="secondary">{member.email}</span></li>)}</ul>
      {circleAttempts.length > 0
        ? <p>This run checked {circleAttempts.length} recipient{circleAttempts.length === 1 ? '' : 's'} against the circle above — {circleAttempts.map(d => `${d.subject.toLowerCase()}: ${d.verdict}`).join('; ')}. The circle itself is unchanged: nothing on this route can add to it.</p>
        : <p className="secondary">This run made no attempt that touched the circle.</p>}
    </section>
  </section>;
}
