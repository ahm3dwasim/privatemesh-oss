'use client';
import type { Session } from '@/lib/v2-types';
import { limitsModeLabel } from './Shell';
import type { LimitsMode } from '@/lib/v2-types';

/**
 * The first three minutes: connect something, say how much it may do, teach it one thing.
 *
 * It replaces Today only while there is no history at all, because a setup screen that outlives its
 * usefulness is the most common way an app starts to feel like a demo. Each step reports its own
 * real state from the session and the limits, so a step that is already done says so rather than
 * asking again.
 */
export function FirstRun({ session, mode, chosen, taught, sample, onConnect, onLimits, onTeach, onDone }: {
  session: Session | null;
  mode: LimitsMode | null;
  /** Whether the owner has ANSWERED, as opposed to the server reporting its default. `limits.mode`
   *  is never null, so treating it as the answer ticked this step off before it was taken. */
  chosen: boolean;
  /** Whether at least one active skill exists — the real signal a rule has been taught and kept,
   *  read from the same library Skills itself reads, so this step can never claim a use it hasn't. */
  taught: boolean;
  sample: boolean;
  onConnect: () => void;
  onLimits: () => void;
  onTeach: () => void;
  /** The owner's own "Start using PrivateMesh" — a real exit from setup, not a checklist that just
   *  goes quiet once every step is done. */
  onDone: () => void;
}) {
  const connected = session?.connections.filter(c => c.state === 'connected').length ?? 0;
  const total = session?.connections.length ?? 0;
  const steps = [
    {
      title: 'Connect Google Calendar and Gmail',
      body: sample
        // Honest about what connecting WOULD do, not a step that claims there is nothing to do —
        // the fix for a first step that offered no real action and no real state.
        ? 'Sample workspace: connecting would let the assistant read the one meeting thread it is asked about, and update that one invite after you approve it — nothing else. Nothing is connected for real here.'
        : 'The assistant reads the one meeting thread it was asked about, and nothing else.',
      done: sample || (total > 0 && connected === total),
      action: sample ? 'See what connecting would do' : 'Connect Google Calendar and Gmail',
      run: onConnect,
    },
    {
      title: 'Choose how much it may do on its own',
      body: mode
        ? `${chosen ? 'Right now' : 'The default until you choose'}: ${limitsModeLabel[mode]}. You can change this at any time, and a change never runs a plan already waiting for you.`
        : 'Ask every time, act only within six written limits, or watch only and execute nothing.',
      done: chosen,
      action: 'Open limits',
      run: onLimits,
    },
    {
      title: 'Teach it one rule',
      body: 'Write a sentence about which times don’t work for you. You read its worked examples before anything is kept — and a taught rule can only ever remove options.',
      done: taught,
      action: 'Teach a rule',
      run: onTeach,
    },
  ];
  const doneCount = steps.filter(step => step.done).length;
  const complete = steps.every(step => step.done);
  // The completion sentence is a claim about the background routine, so it is read from the routine
  // rather than assumed from the three steps above it. Connecting the tools and choosing limits does
  // not start anything: the routine is its own switch (`session.routine.enabled`), and it can be
  // paused without any step here changing. "Setup complete. The routine keeps reading your meeting
  // email" over a routine that is off is the one sentence on this screen that promises work which
  // will not happen.
  const routineOn = session?.routine.enabled === true && session?.routine.paused === false;
  return <section className="first-run" aria-label="Getting started">
    {/* One sentence the rest of the product is built to prove, stated once, plainly, before any of
        the individual concepts (plans, boundaries, agreements, skills, limits) show up on their own
        screens and have to be related back to each other by the reader. */}
    <p className="mental-model">The assistant proposes a plan. Your limits decide what it may do on its own. You approve the rest.</p>
    {/* Explicit progress, not just a checklist — the fix for a three-step flow with no sense of how
        far along it is. */}
    <div className="setup-progress" role="status">
      <p>Step {Math.min(doneCount + 1, steps.length)} of {steps.length}{complete ? ' · done' : ''}</p>
      <div className="progress-track"><div className="progress-fill" style={{ width: `${(doneCount / steps.length) * 100}%` }} /></div>
    </div>
    <ol className="steps">
      {steps.map((step, index) => <li key={step.title} className={step.done ? 'done' : ''}>
        <span className="step-index" aria-hidden="true">{step.done ? '✓' : index + 1}</span>
        <div>
          <h2>{step.title}</h2>
          <p>{step.body}</p>
          {/* The filled control belongs to the first step still to do, not to step one — a finished
              step that still shouts is what makes a setup screen feel like a brochure. */}
          <button className={!step.done && steps.slice(0, index).every(earlier => earlier.done) ? 'primary' : ''} onClick={step.run}>{step.action}</button>
        </div>
      </li>)}
    </ol>
    {/* A real ending: pressing it is what actually leaves setup, instead of hoping today's data
        happens to make the screen go away on its own. */}
    {complete && <section className="card first-run-complete" role="status">
      <h2>Setup complete.</h2>
      <p>{routineOn
        ? 'The routine keeps reading your meeting email in the background. From here on, Today is a record of what it did, not a setup task.'
        : 'The routine is not switched on, so nothing is being read in the background yet. Turn on “Meeting changes” in Settings whenever you want it to start — until then, changes reach you when you open them.'}</p>
      <button className="primary" onClick={onDone}>Start using PrivateMesh</button>
    </section>}
    <details className="first-run-security"><summary>How your boundaries are protected</summary>
      <p>Nothing untrusted — an email, a model’s answer, or code it wrote — can widen what the assistant may do or reveal. Only trusted code can cause an effect, and only within the limits you chose above.</p>
    </details>
  </section>;
}
