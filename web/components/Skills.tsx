'use client';
import type { ReactNode } from 'react';
import type { Change, SkillSummary } from '@/lib/v2-types';
import { time } from './Day';

const date = (value: string | null) => value && Number.isFinite(Date.parse(value))
  ? new Date(value).toLocaleDateString('en-GB', { timeZone: 'Europe/London', day: 'numeric', month: 'short', year: 'numeric' })
  : null;

/** The sentence this whole screen exists to make unmissable. It is stated once per skill, in the
 *  same words every time, because a rule the owner cannot state back is a rule they did not give. */
export const NARROWING_LAW = 'A taught rule can only remove options. It can never add one, move one or send anything.';

/**
 * What one taught rule actually did, drawn from the plan's own `rejected` list.
 *
 * The removed options are the entries whose `by` names this skill — the Teach Mode contract's own
 * field — so the denominator is "the times this skill saw": the one the plan kept plus the ones this
 * skill took away. Nothing is inferred from the library entry alone: an installed skill that removed
 * nothing today says so, because "active" and "used" are different facts.
 */
/** The best change to show for one skill: the most recently updated one it actually narrowed
 *  something on, so an owner who taught a rule days ago still sees the win at first view instead of
 *  "nothing has been prepared" until they happen to open today's own change. Falls back to whichever
 *  change is open right now, so a skill that has genuinely narrowed nothing yet still says so. */
function exemplarFor(skill: SkillSummary, changes: Change[], current: Change | null): Change | null {
  const withNarrowing = changes.filter(c => (c.proposal?.rejected ?? []).some(option => option.by === skill.name));
  if (withNarrowing.length)
    return [...withNarrowing].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))[0]!;
  return current;
}

export function Narrowing({ skill, change, timezone }: { skill: SkillSummary; change: Change | null; timezone: string }) {
  const proposal = change?.proposal;
  const removed = (proposal?.rejected ?? []).filter(option => option.by === skill.name);
  if (!proposal)
    return <p className="secondary">No plan has been prepared since you taught this. It has removed nothing.</p>;
  const kept = time(proposal.start, timezone);
  const offered = removed.length + 1;
  return <div className="narrowing">
    <p className="narrowing-head">{removed.length === 0
      ? 'It removed none of the times offered on today’s change.'
      : <>Removed <strong>{removed.length} of the {offered} times</strong> it was offered on today’s change.</>}</p>
    <ul className="slots">
      <li className="slot kept"><span className="slot-time tabular">{kept}</span><span className="slot-state">kept</span></li>
      {removed.map((option, index) => <li className="slot removed" key={`${option.name}-${index}`}>
        <span className="slot-time tabular"><s>{option.name}</s></span>
        <span className="slot-state">removed by this rule</span>
      </li>)}
    </ul>
    <p className="secondary">{kept} still had to pass every trusted check on its own. A rule cannot put a time back.</p>
  </div>;
}

export function Skills({ skills, change, changes, timezone, disabled, owner, onRevoke, teach }: {
  skills: SkillSummary[];
  change: Change | null;
  /** Every change this workspace knows about, live and historical — how a skill taught days ago
   *  still shows its narrowing at first view instead of only after today's own change is prepared. */
  changes: Change[];
  timezone: string;
  disabled: boolean;
  owner: boolean;
  onRevoke: (name: string) => void;
  teach: ReactNode;
}) {
  const active = skills.filter(s => s.state === 'active');
  const revoked = skills.filter(s => s.state !== 'active');
  return <section className="skills-page" aria-label="Skills">
    <div className="teach-lead">{teach}</div>

    <div className="law-band">
      <span className="law-glyph" aria-hidden="true">⊖</span>
      <p>{NARROWING_LAW}</p>
    </div>

    <h2>Learned scheduling rules</h2>
    {skills.length === 0 && <p className="secondary">Nothing taught yet. Write a rule above and you will see its worked examples before anything is kept.</p>}

    <div className="skill-grid">
      {[...active, ...revoked].map(skill => <article className={`card skill-card ${skill.state}`} key={skill.identity}>
        <div className="skill-head">
          {/* The NAME is the title — the fix for two cards that once showed the identical rule text
              as their only heading and read as duplicate data. The rule stays underneath, verbatim. */}
          <h3>{skill.name}</h3>
          <span className={`status-chip tone-${skill.state === 'active' ? 'done' : 'quiet'}`}>{skill.state}</span>
        </div>
        {skill.rule && <p className="taught-rule">{skill.rule}</p>}
        <p className="secondary">{skill.purpose_plain}</p>
        {skill.state === 'active'
          ? <Narrowing skill={skill} change={exemplarFor(skill, changes, change)} timezone={timezone} />
          : <p className="secondary">Disabled for future plans. Earlier action records remain.</p>}
        <p className="skill-meta secondary tabular">v{skill.version} · learned {date(skill.learned_at) ?? 'unavailable'} · used {skill.uses} {skill.uses === 1 ? 'time' : 'times'} · last used {date(skill.last_used_at) ?? 'not yet'}</p>
        <details><summary>Skill identity</summary><code className="skill-identity">{skill.identity}</code></details>
        {/* The visible label is about the rule the owner reads on the card; the accessible name still
            carries the skill's own name, which is what identifies it in the library. */}
        {skill.state === 'active' && <button aria-label={`Revoke ${skill.name}`} disabled={disabled || !owner} onClick={() => onRevoke(skill.name)}>Revoke this {skill.rule ? 'rule' : 'skill'}</button>}
      </article>)}
    </div>
    {!owner && <p>Sign in as the owner to teach or revoke a rule.</p>}
  </section>;
}
