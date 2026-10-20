import { createDemoClient } from './demo-api';
import { ApiError } from './v2-types';

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

afterEach(() => { vi.unstubAllGlobals(); });

test('start posts to /v2/demo/session and returns the session state', async () => {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => json(200, { session_id: 'sid-1', mode: 'demo', real: [], simulated: [], notice: '',
    budget: { calls_left: 24, degraded: null },
    caps: { session_usd: 0.05, session_calls: 24, rate_requests: 10, rate_window_s: 60, total_usd: 1, daily_usd: 0.25, session_ttl_s: 1800 },
    spend: {}, inbox: [] }));
  vi.stubGlobal('fetch', fetchMock);
  const client = createDemoClient('');
  const session = await client.start();
  expect(session.session_id).toBe('sid-1');
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0]!;
  expect(url).toBe('/v2/demo/session');
  expect(init?.method).toBe('POST');
  expect(init?.credentials).toBe('omit');
});

test('state reads GET /v2/demo/session/{id}', async () => {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => json(200, { session_id: 'sid-1', mode: 'demo', real: [], simulated: [], notice: '',
    budget: { calls_left: 10, degraded: null },
    caps: { session_usd: 0.05, session_calls: 24, rate_requests: 10, rate_window_s: 60, total_usd: 1, daily_usd: 0.25, session_ttl_s: 1800 },
    spend: {}, inbox: [] }));
  vi.stubGlobal('fetch', fetchMock);
  const client = createDemoClient('');
  const session = await client.state('sid-1');
  expect(session.budget.calls_left).toBe(10);
  const [url, init] = fetchMock.mock.calls[0]!;
  expect(url).toBe('/v2/demo/session/sid-1');
  expect(init?.method).toBe('GET');
});

test('sendEmail posts the message id and body to the email route', async () => {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => json(200, { session_id: 'sid-1', change_id: 'ch-1', budget: { calls_left: 23, degraded: null },
    spend: {}, change: null, model_view: null, decisions: [] }));
  vi.stubGlobal('fetch', fetchMock);
  const client = createDemoClient('');
  const result = await client.sendEmail('sid-1', { message_id: 'msg-1', body_text: 'attack text' });
  expect(result.change_id).toBe('ch-1');
  const [url, init] = fetchMock.mock.calls[0]!;
  expect(url).toBe('/v2/demo/session/sid-1/email');
  expect(JSON.parse(String(init?.body))).toEqual({ message_id: 'msg-1', body_text: 'attack text' });
});

test('sendEmailCompromised posts the message id and body to the compromised route', async () => {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => json(200, { session_id: 'sid-1', change_id: 'ch-1', budget: { calls_left: 24, degraded: null },
    spend: {}, change: null, model_view: null, decisions: [], compromised: true, attempted: ['addressed the reply to: attacker@example.com'] }));
  vi.stubGlobal('fetch', fetchMock);
  const client = createDemoClient('');
  const result = await client.sendEmailCompromised('sid-1', { message_id: 'msg-1', body_text: 'attack text' });
  expect(result.compromised).toBe(true);
  expect(result.attempted).toEqual(['addressed the reply to: attacker@example.com']);
  // Zero calls spent is a claim about the SESSION the caller reads back, not about this route:
  // the budget in the response is whatever the server sent, unchanged from before this call.
  expect(result.budget.calls_left).toBe(24);
  const [url, init] = fetchMock.mock.calls[0]!;
  expect(url).toBe('/v2/demo/session/sid-1/email/compromised');
  expect(JSON.parse(String(init?.body))).toEqual({ message_id: 'msg-1', body_text: 'attack text' });
});

test('an error response is surfaced as an ApiError with the server code and status', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_url: string, _init?: RequestInit) => json(503, { code: 'demo_unavailable', detail: 'the demo has reached its session limit' })));
  const client = createDemoClient('');
  await expect(client.start()).rejects.toMatchObject({ status: 503, code: 'demo_unavailable', message: 'the demo has reached its session limit' });
});

test('rate_limited surfaces as 429 with the code intact', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_url: string, _init?: RequestInit) => json(429, { code: 'rate_limited', detail: 'wait and try again' })));
  const client = createDemoClient('');
  await expect(client.start()).rejects.toMatchObject({ status: 429, code: 'rate_limited' });
});

test('demo_empty_email and demo_limit are 422s from the email route', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_url: string, _init?: RequestInit) => json(422, { code: 'demo_empty_email', detail: 'the email body is empty — nothing to run' })));
  const client = createDemoClient('');
  await expect(client.sendEmail('sid-1', { message_id: 'msg-1', body_text: '   ' }))
    .rejects.toMatchObject({ status: 422, code: 'demo_empty_email' });
});

test('an unreachable backend is a network_error, not an unhandled rejection shape', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
  const client = createDemoClient('');
  const error = await client.start().catch(e => e);
  expect(error).toBeInstanceOf(ApiError);
  expect((error as ApiError).code).toBe('network_error');
});

test('a response body that is not JSON still produces a readable ApiError', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('not json', { status: 500 })));
  const client = createDemoClient('');
  await expect(createDemoClient('').start()).rejects.toMatchObject({ status: 500, code: 'request_failed' });
});

test('base URL is prefixed onto every route', async () => {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => json(200, { session_id: 'sid-9', mode: 'demo', real: [], simulated: [], notice: '',
    budget: { calls_left: 1, degraded: null },
    caps: { session_usd: 0.05, session_calls: 24, rate_requests: 10, rate_window_s: 60, total_usd: 1, daily_usd: 0.25, session_ttl_s: 1800 },
    spend: {}, inbox: [] }));
  vi.stubGlobal('fetch', fetchMock);
  await createDemoClient('https://demo.example').state('sid-9');
  expect(fetchMock.mock.calls[0]![0]).toBe('https://demo.example/v2/demo/session/sid-9');
});
