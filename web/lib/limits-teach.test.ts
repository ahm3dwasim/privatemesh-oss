import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createSampleAdapter, SAMPLE_CHANGE_ID as id } from './sample';
import { createLiveClient } from './api';
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-21T09:00:00Z')); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
it('defaults to asking; choosing limits never runs an already prepared change', async () => {
  const api = createSampleAdapter();
  expect(await api.limits()).toMatchObject({ mode: 'ask_always', updated_at: null });
  expect((await api.limits()).clauses).toHaveLength(7);
  await api.prepare(id); vi.advanceTimersByTime(2200); await api.change(id);
  await api.setLimits('within_limits');
  expect((await api.change(id)).stage).toBe('proposal_ready');
  expect((await api.change(id)).executed_by).toBeNull();
});
it('runs a newly prepared sample within limits and records all seven clauses before execution', async () => {
  const api = createSampleAdapter(); await api.setLimits('within_limits');
  await api.prepare(id); vi.advanceTimersByTime(2200); await api.change(id);
  vi.advanceTimersByTime(1700);
  expect(await api.change(id)).toMatchObject({ stage: 'done', executed_by: 'limit' });
  const rows = await api.decisions(id);
  expect(rows.filter(r => r.stage === 'approve').map(r => r.rule)).toEqual((await api.limits()).clauses.map(c => c.text));
  expect(rows.filter(r => r.stage === 'approve').every(r => r.source === 'owner' && r.verdict === 'allowed')).toBe(true);
  expect(rows.findIndex(r => r.stage === 'execute')).toBeGreaterThan(rows.findIndex(r => r.stage === 'approve'));
});
it('off refuses approval; a failed limit names its clause and retains all checks', async () => {
  const api = createSampleAdapter('limit_handoff'); await api.setLimits('within_limits');
  await api.prepare(id); vi.advanceTimersByTime(2200);
  expect(await api.change(id)).toMatchObject({ stage: 'proposal_ready', executed_by: null, blocked_reason: expect.stringContaining('Clause 4') });
  // Seven now, not six: clause 7 is evaluated on every bundle and passes CONDITIONALLY here —
  // this plan replies to the clinic front desk, who is on the care circle and listed for
  // `schedule`. A clause that only appeared on care changes would be a clause the owner could not
  // see holding, and "did it run?" would be unanswerable from the screen.
  expect((await api.decisions(id)).filter(r => r.stage === 'approve')).toHaveLength(7);
  await api.setLimits('off');
  await expect(api.approve(id, 1)).rejects.toMatchObject({ code: 'limits_off' });
});
it('teaches, reviews examples bound to identity, installs and revokes a rule without effects', async () => {
  const api = createSampleAdapter(); const { learning_id } = await api.teach('Never before 10am');
  expect((await api.learning(learning_id)).stage).toBe('generating');
  vi.advanceTimersByTime(600); expect((await api.learning(learning_id)).stage).toBe('testing');
  vi.advanceTimersByTime(1100); expect((await api.learning(learning_id)).stage).toBe('reviewing');
  vi.advanceTimersByTime(700); const learning = await api.learning(learning_id);
  expect(learning.rule).toBe('Never before 10am'); expect(learning.examples.some(e => !e.kept)).toBe(true);
  expect(learning.manifest!.examples).toEqual(learning.examples);
  await expect(api.installSkill(learning_id, 'wrong')).rejects.toMatchObject({ code: 'stale_identity' });
  await api.installSkill(learning_id, learning.manifest!.identity);
  // The sample seeds one taught rule already active, so the just-taught one is found by its own
  // rule text rather than assumed to be the only entry.
  const skill = (await api.skills()).find(s => s.rule === learning.rule);
  expect(skill?.rule).toBe(learning.rule);
  await api.revokeSkill(skill!.name); expect((await api.skills()).find(s => s.rule === learning.rule)?.state).toBe('revoked');
  expect((await api.change(id)).stage).toBe('detected');
});
it.each([['', 'empty_rule'], ['x'.repeat(281), 'rule_too_long'], ['email my boss', 'rule_asks_for_an_action']])('refuses %s before creating a skill', async (rule, code) => {
  const api = createSampleAdapter();
  const before = await api.skills();
  await expect(api.teach(rule)).rejects.toMatchObject({ status: 422, code });
  expect(await api.skills()).toEqual(before);
});
it('uses the exact HTTP contracts', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) }); vi.stubGlobal('fetch', fetch);
  const api = createLiveClient(''); await api.limits(); await api.setLimits('off'); await api.teach('Never before 10am');
  expect(fetch.mock.calls.map(c => [c[0], c[1].method, c[1].body])).toEqual([
    ['/v2/limits', 'GET', undefined], ['/v2/limits', 'POST', JSON.stringify({ mode: 'off' })], ['/v2/teach', 'POST', JSON.stringify({ rule: 'Never before 10am' })],
  ]);
});

it('no bundle produces no clause verdicts, and unknown modes write nothing', async () => {
  const api = createSampleAdapter('no_safe_fit');
  await expect(api.setLimits('typo' as never)).rejects.toMatchObject({ code: 'unknown_limits_mode' });
  expect((await api.limits()).mode).toBe('ask_always');
  await api.setLimits('within_limits'); await api.prepare(id); vi.advanceTimersByTime(2200);
  expect((await api.change(id)).stage).toBe('no_safe_fit');
  expect((await api.decisions(id)).filter(row => row.stage === 'approve')).toEqual([]);
});
