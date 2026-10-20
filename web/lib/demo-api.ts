import { ApiError } from './v2-types';
import type { DemoResult, DemoSessionState } from './demo-types';

/**
 * The transport for the public demo. Unlike `lib/api.ts`'s owner client, these four routes carry
 * no credential and are meant to be reached from anywhere a judge's browser sits — `docs/API_V2.md`
 * is explicit that they are public by design — so there is no loopback/allowlist gate here: that
 * gate exists to protect the OWNER token, and a demo session never holds one.
 *
 * `NEXT_PUBLIC_API_BASE_URL` is the same variable the live client reads, because both are "where
 * is the backend" for one build — a build only ever points at one server.
 */
export function createDemoClient(base = process.env.NEXT_PUBLIC_API_BASE_URL ?? '') {
  async function request<T>(path: string, body?: unknown, method = body === undefined ? 'GET' : 'POST'): Promise<T> {
    let response: Response;
    try {
      response = await fetch(`${base}/v2/demo${path}`, {
        method, credentials: 'omit', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    }
    catch {
      throw new ApiError(0, 'The demo could not be reached. Check your connection and try again.', 'network_error');
    }
    if (!response.ok) {
      const error = await response.json().catch(() => ({})) as { detail?: string; code?: string };
      throw new ApiError(response.status, error.detail ?? `Request failed (${response.status})`, error.code);
    }
    return response.json() as Promise<T>;
  }
  return {
    start: (): Promise<DemoSessionState> => request('/session', undefined, 'POST'),
    state: (sessionId: string): Promise<DemoSessionState> => request(`/session/${encodeURIComponent(sessionId)}`),
    sendEmail: (sessionId: string, value: { message_id: string; body_text: string }): Promise<DemoResult> =>
      request(`/session/${encodeURIComponent(sessionId)}/email`, value),
    // The SAME email, through the red-team corpus's own fully-compliant scripted model instead —
    // `docs/API_V2.md` § "The compromised run". Zero spend, zero model calls; still one request
    // against the demo's shared rate limit.
    sendEmailCompromised: (sessionId: string, value: { message_id: string; body_text: string }): Promise<DemoResult> =>
      request(`/session/${encodeURIComponent(sessionId)}/email/compromised`, value),
  };
}

export type DemoClient = ReturnType<typeof createDemoClient>;
