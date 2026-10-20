import { ApiError, type Learning, type SkillSummary, type Overreach } from './v2-types';
export const sampleOverreach: Overreach = { skill: 'Meeting option evaluator', attempted: 'Read the protected event title', blocked_by: 'manifest', detail: 'Version 1 requested private event details. Only busy intervals and buffers were permitted; no private title was returned.' };
/** Synthetic evidence, never a claim that generated code or a reviewer actually ran. */
export function sampleLearning(id: string, changeId: string): Learning {
  return {
    id, change_id: changeId, skill_name: 'Meeting option evaluator', purpose_plain: 'Rank offered meeting times and checked venues while preserving your notice and buffers.', stage: 'generating',
    rule: null, examples: [], attempts: [],
    manifest: { reads: ['Offered slots from the selected thread', 'Public venue evidence', 'Minimum notice, busy intervals and buffers'], proposes: ['Calendar update suggestion', 'Email reply suggestion'], never: ['Read private event titles or locations', 'Send an email or update an invite', 'Access unrelated threads'], expires: new Date(Date.now() + 30 * 86400000).toISOString(), identity: 'sha256:' + 'd2'.repeat(32) }, code_url: null,
  };
}
export const sampleAttempts: Learning['attempts'] = [
  { version: 1, tests: { passed: 7, failed: 1 }, held_out: { passed: 2, failed: 1 }, violations: [{ kind: 'private_context_read', detail: sampleOverreach.detail }], review: { verdict: 'reject', reasons: ['The evaluator asks for a private title it does not need. Use only permitted busy intervals.'], reviewer_model: 'Synthetic reviewer · no model call' } },
  { version: 2, tests: { passed: 9, failed: 0 }, held_out: { passed: 4, failed: 0 }, violations: [], review: { verdict: 'approve', reasons: ['Ranks only supplied options, respects buffers, and returns suggestions without effects.'], reviewer_model: 'Synthetic reviewer · no model call' } },
];
export function requireInstallable(learning: Learning, identity: string): asserts learning is Learning & { manifest: NonNullable<Learning['manifest']> } {
  if (learning.stage !== 'ready_for_review' || !learning.manifest || learning.manifest.identity !== identity || Date.parse(learning.manifest.expires) <= Date.now())
    throw new ApiError(409, 'The skill changed or expired. Review its current permission page.', 'stale_identity');
}
export function installedSummary(learning: Learning & { manifest: NonNullable<Learning['manifest']> }): SkillSummary {
  return { rule: learning.rule, name: learning.skill_name, purpose_plain: learning.purpose_plain, version: 2, identity: learning.manifest.identity, state: 'active', learned_at: new Date().toISOString(), last_used_at: null, uses: 0 };
}
