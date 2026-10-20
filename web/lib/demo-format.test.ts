import { formatDemoInstant, formatDemoRange } from './demo-format';

test('formatDemoInstant renders a day and a 24-hour clock, in Europe/London', () => {
  expect(formatDemoInstant('2026-09-22T13:00:00+01:00')).toBe('Tue 22 Sep, 13:00');
});

test('formatDemoInstant answers blank for nothing to format', () => {
  expect(formatDemoInstant(null)).toBe('');
  expect(formatDemoInstant(undefined)).toBe('');
  expect(formatDemoInstant('')).toBe('');
  expect(formatDemoInstant('not-a-date')).toBe('');
});

test('formatDemoRange says the day once when both ends share it', () => {
  expect(formatDemoRange('2026-09-22T13:00:00+01:00', '2026-09-22T13:45:00+01:00'))
    .toBe('Tue 22 Sep, 13:00 → 13:45');
});

test('formatDemoRange with no end just formats the start', () => {
  expect(formatDemoRange('2026-09-22T13:00:00+01:00', null)).toBe('Tue 22 Sep, 13:00');
});

test('formatDemoRange with no start but an end answers the end alone', () => {
  expect(formatDemoRange(null, '2026-09-22T13:45:00+01:00')).toBe('Tue 22 Sep, 13:45');
});

test('formatDemoRange never renders a raw ISO string', () => {
  const out = formatDemoRange('2026-09-22T09:30:00+01:00', '2026-09-22T12:45:00+01:00');
  expect(out).not.toMatch(/\d{4}-\d{2}-\d{2}T/);
  expect(out).not.toContain('+01:00');
});
