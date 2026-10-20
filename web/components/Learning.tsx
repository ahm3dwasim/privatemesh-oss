'use client';
import { useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import type { Change, Learning, Overreach, SkillSummary } from '@/lib/v2-types';
const date = (value: string) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleDateString('en-GB', { timeZone: 'Europe/London', day: 'numeric', month: 'short', year: 'numeric' }) : 'Unavailable';

/** A single, plain-language sentence for the four example rules this sample knows, so the owner
 *  reads a confirmed interpretation before they approve — not just a pile of worked examples they
 *  have to generalise from themselves. Live mode generates its own rule from the owner's own
 *  sentence, so an unrecognised one falls back to a sentence that is still true and still plain. */
const knownInterpretations: Record<string, string> = {
  'Nothing after 15:30': 'Do not suggest a meeting starting after 15:30.',
  'Never before 10am': 'Do not suggest a meeting starting before 10:00.',
  'Keep Friday afternoons free': 'Do not suggest a meeting on Friday afternoon.',
  '30 minutes after client meetings': 'Do not suggest a time within 30 minutes after a client meeting.',
};
export function interpret(rule: string): string {
  return knownInterpretations[rule] ?? 'Keep only the offered times that fit this rule; never add or move one.';
}
export function RuleExamples({ learning }: { learning: Learning }) {
  if (!learning.rule) return null;
  return <section className="rule-review">
    <h3>Your rule</h3>
    <p className="taught-rule">{learning.rule}</p>
    {/* The rule in the owner's own words is ambiguous on its own — "nothing after 15:30" does not
        say whether a meeting may START before it and run past. This sentence is the one place that
        ambiguity is resolved, in the same plain language, before anything is kept. */}
    <p className="confirmed-interpretation"><strong>This means:</strong> {interpret(learning.rule)}</p>
    <h3>The examples you are approving</h3>
    <p>Read these as the model’s interpretation. Checks establish consistency with its code; only you can judge whether it means what you intended.</p>
    {learning.examples.map((example, index) => <article className="record" key={index}><p>{example.input}</p><strong>{example.kept ? 'Kept' : 'Refused'}</strong><p>{example.why}</p></article>)}
  </section>;
}
export function Attempts({ learning }: { learning: Learning }) {
  return <details className="attempt-history"><summary>How this skill was checked · {learning.attempts.length} attempts</summary>{learning.attempts.map(a => <article className="record" key={a.version}><h3>v{a.version} {a.review.verdict === 'reject' ? 'stopped' : 'passed'}</h3><p>Tests: {a.tests.passed} passed, {a.tests.failed} failed. Held-out checks: {a.held_out.passed} passed, {a.held_out.failed} failed.</p>{a.violations.map((v, i) => <p className="attention" key={i}><strong>{v.kind}</strong>: {v.detail}</p>)}<p>Review: {a.review.verdict === 'approve' ? 'Passed' : 'Stopped'} · {a.review.reviewer_model}</p><ul>{a.review.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul></article>)}</details>;
}
export function OverreachList({ events }: { events?: Overreach[] }) {
  if (!events?.length) return null;
  return <section className="overreach"><h2>Stopped at your boundary</h2>{events.map((event, i) => <div className="record" key={i}><strong>{event.skill}: {event.attempted}</strong><p>Blocked by {event.blocked_by}. {event.detail}</p></div>)}</section>;
}
export function LearningPanel({ change, disabled, owner, onPrepared, onInstalled, heading = 'Teach it once. Keep control.' }: { change: Change; disabled: boolean; owner: boolean; onPrepared: (change: Change) => void; onInstalled?: () => void; heading?: string }) {
  const [learning, setLearning] = useState<Learning | null>(null);
  const [permission, setPermission] = useState(false);
  const [installed, setInstalled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const lock = useRef(false);
  const panelHeading = useRef<HTMLHeadingElement>(null);
  const wasPermission = useRef(false);
  const permissionHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { if (permission) permissionHeading.current?.focus(); else if (wasPermission.current) panelHeading.current?.focus(); wasPermission.current = permission; }, [permission]);
  useEffect(() => {
    if (!learning) return;
    let active = true;
    void api.skills().then(skills => { if (active) setInstalled(skills.some(s => s.identity === learning.manifest?.identity && s.state === 'active')); }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [learning?.id]);
  useEffect(() => {
    if (!change.learning_id) return;
    let active = true;
    void api.learning(change.learning_id).then(value => { if (active) setLearning(value); }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [change.learning_id]);
  useEffect(() => {
    if (!learning || ['ready_for_review', 'failed'].includes(learning.stage) || disabled || busy) return;
    let active = true; let pending = false;
    const timer = setInterval(async () => {
      if (pending) return; pending = true;
      try { const next = await api.learning(learning.id); if (active) setLearning(next); }
      catch (e) { if (active) setError(e instanceof Error ? e.message : 'Checks could not refresh.'); }
      finally { pending = false; }
    }, 500);
    return () => { active = false; clearInterval(timer); };
  }, [learning?.id, learning?.stage, disabled, busy]);
  async function act(work: () => Promise<void>) {
    if (lock.current || disabled) return;
    lock.current = true; setBusy(true); setError('');
    try { await work(); } catch (e) { setError(e instanceof Error ? e.message : 'Please try again.'); }
    finally { lock.current = false; setBusy(false); }
  }
  const manifest = learning?.manifest;
  const ready = learning?.stage === 'ready_for_review' && !!manifest;
  const failed = learning?.stage === 'failed';
  const state = change.stage === 'done' ? 3 : ready || change.stage === 'proposal_ready' ? 2 : change.skill_used ? 1 : learning?.stage === 'reviewing' ? 1 : 0;
  return <section className="section learning-panel">
    {error && <p className="alert error" role="alert">{error}</p>}
    {permission && learning && manifest ? <div className="permission-sheet"><p className="eyebrow">Permission</p><h2 ref={permissionHeading} tabIndex={-1}>Keep this skill. Keep control.</h2><p>{learning.purpose_plain}</p><RuleExamples learning={learning} /><div className="permission-columns">{([['Reads', manifest.reads], ['May propose', manifest.proposes], ['Never', manifest.never]] as const).map(([label, items]) => <section key={label}><h3>{label}</h3><ul>{items.map(item => <li key={item}>{item}</li>)}</ul></section>)}</div><p>Expires {date(manifest.expires)}</p><p className="skill-identity">Identity <code>{manifest.identity}</code></p><p className="privacy">Installing allows this skill to compute suggestions. Sending email and changing an invite require your approval or a standing limit that covers the entire bundle.</p><div className="button-row"><button className="primary" disabled={disabled || busy || !owner || !ready || (!!learning.rule && learning.examples.length < 2) || !Number.isFinite(Date.parse(manifest.expires)) || Date.parse(manifest.expires) <= Date.now()} onClick={() => void act(async () => { await api.installSkill(learning.id, manifest.identity); setInstalled(true); setPermission(false); onInstalled?.(); })}>Install this skill</button><button disabled={busy || disabled || !owner} onClick={() => void act(async () => { await api.rejectLearning(learning.id); setLearning(await api.learning(learning.id)); setPermission(false); })}>Discard this skill</button><button disabled={busy} onClick={() => setPermission(false)}>Back to checks</button></div>{!owner && <p>Sign in as the owner to install.</p>}<details><summary>Advanced · generated code</summary>{learning.code_url && /^(https?:\/\/|\/[^/])/.test(learning.code_url) ? <a href={learning.code_url} target="_blank" rel="noreferrer">Inspect generated code</a> : <p>Code preview unavailable in this sample.</p>}</details></div> : <>
      <h2 ref={panelHeading} tabIndex={-1}>{heading}</h2>
      {change.skill_used ? <p className="reuse">Using your skill: <strong>{change.skill_used.name}</strong> (learned {date(change.skill_used.learned_at)})</p> : <p>Learn how to compare meeting options within your boundaries.</p>}
      {(learning || change.skill_used) && <ol className="human-states" aria-label="Task progress">{['Learning your routine', 'Checked', 'Ready for your review', 'Done'].map((label, i) => <li key={label} aria-current={i === state ? 'step' : undefined} className={i < state ? 'reached' : ''}>{label}</li>)}</ol>}
      {failed ? <p role="status">Skill discarded or learning stopped. Nothing was installed by this attempt.</p> : learning && !ready ? <p role="status">{learning.stage === 'revising' ? 'The first attempt stopped at your boundary. Revising with only permitted inputs.' : learning.stage === 'reviewing' ? 'Reviewing the skill. Nothing is sent.' : learning.stage === 'testing' ? 'Testing the skill. Nothing is sent.' : learning.rule ? 'Generating the skill. Nothing is sent.' : 'Learning your routine. Nothing is sent.'}</p> : null}
      {learning && <Attempts learning={learning} />}
      {(!learning || failed) && !change.skill_used && ['detected', 'proposal_ready'].includes(change.stage) && <button disabled={busy || disabled} onClick={() => void act(async () => { const result = await api.learn(change.id); onPrepared({ ...change, learning_id: result.learning_id }); setLearning(await api.learning(result.learning_id)); })}>Teach this routine</button>}
      {ready && !installed && <button className="primary" disabled={disabled || busy} onClick={() => setPermission(true)}>Review skill permissions</button>}
      {installed && <><p role="status">Skill installed. No invite changed and no email sent.</p>{['detected', 'proposal_ready'].includes(change.stage) && <button disabled={busy || disabled} onClick={() => void act(async () => onPrepared(await api.prepare(change.id)))}>Prepare with this skill</button>}</>}
    </>}
  </section>;
}
export function SkillsLibrary({ skills, disabled, revoke }: { skills: SkillSummary[]; disabled: boolean; revoke: (name: string) => void }) {
  return <section className="skills-library"><h2>Your skills</h2><p>Learned evaluators suggest plans. Your approvals and standing limits govern every effect.</p>{!skills.length && <p>No skills installed yet. Teach a routine from Today.</p>}{skills.map(skill => <article className="record" key={skill.identity}><h3>{skill.name} <span className="status-pill">{skill.state}</span></h3><p>{skill.purpose_plain}</p>{skill.rule && <p className="taught-rule">{skill.rule}</p>}<p>v{skill.version} · Learned {date(skill.learned_at)} · {skill.uses} uses · Last used {skill.last_used_at ? date(skill.last_used_at) : 'not yet'}</p><details><summary>Skill identity</summary><code className="skill-identity">{skill.identity}</code></details>{skill.state === 'active' && <button disabled={disabled} onClick={() => revoke(skill.name)}>Revoke {skill.name}</button>}{skill.state === 'revoked' && <p>Disabled for future plans. Earlier action records remain.</p>}</article>)}</section>;
}
