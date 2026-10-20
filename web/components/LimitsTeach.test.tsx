import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { StandingLimits } from './StandingLimits';
import { Teach } from './Teach';
import { LearningPanel, RuleExamples } from './Learning';
import { DecisionLog } from './Decisions';
import { api, ApiError } from '@/lib/api';
import { resetSample, sampleLimitClauses, samplePreview } from '@/lib/sample';
import { sampleLearning } from '@/lib/sample-learning';
beforeEach(() => { resetSample(); vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });
async function click(name: string) { await act(async () => { fireEvent.click(screen.getByRole('button', { name })); }); }
test('a choice is only staged, clauses are verbatim, cancellation does not write', async () => {
  const save = vi.spyOn(api, 'setLimits');
  await act(async () => { render(<StandingLimits disabled={false} owner />); });
  await click('Act within limits');
  expect(save).not.toHaveBeenCalled();
  for (const clause of sampleLimitClauses) expect(screen.getByText(clause.text)).toBeInTheDocument();
  expect(screen.getAllByText("What it can't do:")).toHaveLength(sampleLimitClauses.length);
  expect(document.querySelector('.clauses-exact-panel')).not.toHaveAttribute('open');
  await click('Cancel'); expect(save).not.toHaveBeenCalled();
  await click('Act within limits'); await click('Confirm mode change');
  expect(save).toHaveBeenCalledOnce(); expect((await api.limits()).mode).toBe('within_limits');
});
test('a failed save leaves the current mode unchanged and permits retry', async () => {
  vi.spyOn(api, 'setLimits').mockRejectedValueOnce(new ApiError(403, 'Owner access is required.'));
  await act(async () => { render(<StandingLimits disabled={false} owner />); });
  await click('Watch only'); await click('Confirm mode change');
  expect(screen.getByRole('alert')).toHaveTextContent('Owner access is required');
  expect((await api.limits()).mode).toBe('ask_always');
  await click('Confirm mode change'); expect((await api.limits()).mode).toBe('off');
});
test('teach refusals appear immediately and never enter learning', async () => {
  await act(async () => { render(<Teach disabled={false} owner />); });
  fireEvent.change(screen.getByLabelText("Which times don't work for you?"), { target: { value: 'email my boss' } });
  await click('Teach this rule');
  expect(screen.getByRole('alert')).toHaveTextContent('Refused before any model call');
  expect(screen.queryByText('Review skill permissions')).not.toBeInTheDocument();
});
test('the owner reviews the rule, kept and refused examples and manifest before installation', async () => {
  await act(async () => { render(<Teach disabled={false} owner />); });
  await click('Never before 10am'); await click('Teach this rule');
  await act(async () => { await vi.advanceTimersByTimeAsync(2600); });
  await click('Review skill permissions');
  expect(screen.getByText('Never before 10am')).toBeInTheDocument();
  expect(screen.getByText('An offered time at 09:30')).toBeInTheDocument();
  expect(screen.getByText('Refused', { exact: true })).toBeInTheDocument();
  expect(screen.getAllByText('Kept', { exact: true })).toHaveLength(2);
  expect(screen.getByRole('heading', { name: 'Never' })).toBeInTheDocument();
  const beforeInstall = await api.skills();
  await click('Install this skill');
  const installed = (await api.skills()).find(s => !beforeInstall.some(before => before.identity === s.identity));
  expect(installed?.rule).toBe('Never before 10am');
});
test('learning can load a null manifest while generating', async () => {
  const learning = { ...sampleLearning('pending', ''), manifest: null, rule: 'Never before 10am' };
  vi.spyOn(api, 'learning').mockResolvedValue(learning);
  await act(async () => { render(<LearningPanel change={{ ...samplePreview().change, learning_id: 'pending' }} disabled={false} owner onPrepared={() => {}} />); });
  expect(screen.getByRole('status')).toHaveTextContent('Generating');
  expect(screen.queryByText('Review skill permissions')).not.toBeInTheDocument();
});
test('limit trail renders only matched catalogue clauses, never raw audit fields', () => {
  const clause = sampleLimitClauses[3]!;
  const row = { seq: 0, at: '', stage: 'approve' as const, source: 'owner' as const, untrusted: false, subject: 'PRIVATE_TITLE', detail: 'PRIVATE_LOCATION', verdict: 'refused' as const, rule: clause.text };
  const { container } = render(<DecisionLog decisions={[row, { ...row, seq: 1, rule: 'PRIVATE_RULE' }]} clauses={sampleLimitClauses} />);
  expect(container).toHaveTextContent(clause.text);
  expect(container).not.toHaveTextContent(/PRIVATE_TITLE|PRIVATE_LOCATION|PRIVATE_RULE/);
});
test('example strings render as text', () => {
  const learning = { ...sampleLearning('x', ''), rule: '<img src=x>', examples: [{ input: '<script>bad()</script>', kept: false, why: 'No time was offered.' }] };
  const { container } = render(<RuleExamples learning={learning} />);
  expect(container.querySelector('script, img')).toBeNull();
});
