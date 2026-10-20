'use client';
import { useRef, useState } from 'react';
import { api, ApiError } from '@/lib/api';
import { LearningPanel } from './Learning';
const examples = ['Nothing after 15:30', 'Never before 10am', 'Keep Friday afternoons free', '30 minutes after client meetings'];
export function Teach({ disabled, owner, onInstalled }: { disabled: boolean; owner: boolean; onInstalled?: () => void }) {
  const [rule, setRule] = useState('');
  const [learningId, setLearningId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [refused, setRefused] = useState(false);
  const lock = useRef(false);
  async function submit() {
    if (lock.current || disabled || !owner) return;
    lock.current = true; setBusy(true); setError(''); setRefused(false);
    try { const result = await api.teach(rule); setLearningId(result.learning_id); }
    catch (e) { setError(e instanceof Error ? e.message : 'The rule could not be submitted.'); setRefused(e instanceof ApiError && ['empty_rule', 'rule_too_long', 'rule_asks_for_an_action'].includes(e.code)); }
    finally { lock.current = false; setBusy(false); }
  }
  return <section className="teach-panel"><h2>Teach it once</h2><p className="lead">Write a sentence about which times don’t work for you. You read its worked examples before anything is kept.</p>
    {!learningId ? <form onSubmit={e => { e.preventDefault(); void submit(); }}>
      <label htmlFor="teach-rule">Which times don&apos;t work for you?</label>
      <input id="teach-rule" value={rule} placeholder="Nothing after 15:30" onChange={e => { setRule(e.target.value); setError(''); }} aria-describedby="teach-help" disabled={busy || disabled || !owner} />
      <p id="teach-help" className="secondary">Up to 280 characters. Say which times don&apos;t work — a taught rule only ever removes an offered time, never adds one.</p>
      <div className="teach-examples">{examples.map(example => <button type="button" key={example} aria-pressed={rule === example} disabled={busy || disabled || !owner} onClick={() => { setRule(example); setError(''); }}>{example}</button>)}</div>
      <button className="primary" disabled={busy || disabled || !owner}>{busy ? 'Submitting rule…' : 'Teach this rule'}</button>
    </form> : <LearningPanel key={learningId} heading="Read what it means, then approve" change={{ id: '', version: 0, stage: 'stopped', request_excerpt: '', proposal: null, blocked_reason: null, blocked_attempts: 0, executed_by: null, updated_at: '', learning_id: learningId }} disabled={disabled} owner={owner} onPrepared={() => {}} onInstalled={onInstalled} />}
    {error && <div className="alert error" role="alert">{refused && <strong>Refused before any model call. </strong>}{error}</div>}
    {learningId && <button disabled={disabled || busy} onClick={() => { setLearningId(null); setRule(''); }}>Teach another rule</button>}
    {!owner && <p>Sign in as the owner to teach a rule.</p>}
  </section>;
}
