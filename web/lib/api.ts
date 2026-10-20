import type { DecisionClient } from './decisions';
import { createSampleAdapter } from './sample';
import { apiRequestOrigin, isLoopbackOrigin, remoteApiOriginAllowlist } from './urls';
import { ApiError, type Api, type Session } from './v2-types';
export * from './v2-types';
// A third value, 'demo', selects the public "Try it live" landing (`components/DemoApp.tsx`) —
// see `app/page.tsx`. It is not a fourth `Api` implementation: the demo talks to
// `lib/demo-api.ts`'s own client against `/v2/demo/*`, and `api` below still resolves to the
// sample adapter for it, because `/tour` (the sample workspace) is reachable from every build and
// needs its own fixture data regardless of what the root page shows.
export const apiMode = process.env.NEXT_PUBLIC_API_MODE === 'live' ? 'live'
  : process.env.NEXT_PUBLIC_API_MODE === 'demo' ? 'demo' : 'sample';
/**
 * The owner credential is a RUNTIME VALUE. It is entered by the owner in the running app and held in
 * memory for that page's lifetime. It is never read from the environment, never persisted and never
 * bundled.
 *
 * The version this replaces read `NEXT_PUBLIC_OWNER_TOKEN` behind a `process.env.NODE_ENV`
 * check — "not a production build, so the browser may have the key". Both halves of that were
 * wrong. `NEXT_PUBLIC_*` is INLINED AT BUILD TIME, so the value is not fetched at runtime and not
 * gated by anything: it is bytes in a file that anyone who loads the page already has. And
 * `NODE_ENV` does not describe who can reach a build — `next build && next start` is a production
 * build served to whoever can reach the port, and `next dev` bound to 0.0.0.0 is a development
 * build served to the same people. The condition being asked was "is this a real build"; the
 * question that decides whether a credential is safe to send is "who can read this artifact", and
 * NODE_ENV cannot answer it. So there is no build-time branch any more, because there is no
 * build-time value.
 *
 * `npm run bundle-check` reads the built artifact after a build made with the variable set and fails
 * if the value is in it, which is what makes this a checked property rather than a comment.
 *
 * Two properties this keeps from before: a client with no token sends no `Authorization` header at
 * all (the gate then refuses, which is the correct outcome and is rendered as "not available", never
 * as "nothing was sent"), and the token is never written to storage — closing the tab forgets it.
 */
let ownerTokenValue: string | null = null;
export function setOwnerToken(value: string | null | undefined): void {
  const trimmed = typeof value === 'string' ? value.trim() : '';
  ownerTokenValue = trimmed ? trimmed : null;
}
export function ownerToken(): string | null {
  return ownerTokenValue;
}
/**
 * The ONE condition under which the browser may send the owner credential, and it is not about the
 * build: it is about where the bytes go. A non-loopback origin puts the token on a network the
 * owner does not control, in a plain `Authorization` header, from a client that is documented as a
 * local tool.
 *
 * Two things had to be true for this to be a decision rather than a gesture, and the previous
 * version had neither:
 *
 * 1. **The origin is RESOLVED, not assumed.** An empty base used to be waved through as
 *    "same-origin", which is the same sentence as "wherever this page was served from" — so a build
 *    with no API base, hosted at `https://app.example`, sent the bearer to `https://app.example`.
 *    The question asked now is `apiRequestOrigin(base, pageOrigin)`: the origin the browser will
 *    actually use.
 * 2. **A remote origin is NAMED.** `NEXT_PUBLIC_ALLOW_REMOTE_API=1` alone approved "anywhere", so
 *    the flag plus a base URL that later changed sent the token somewhere nobody had approved.
 *    The opt-in now only unlocks the allowlist, and the resolved origin must be an EXACT entry in
 *    `NEXT_PUBLIC_API_ORIGIN_ALLOWLIST` — scheme, host and port. No wildcard, no suffix match.
 */
export function remoteApiOptIn(): boolean {
  return process.env.NEXT_PUBLIC_ALLOW_REMOTE_API === '1';
}
export function transportRefusal(base: string, pageOrigin = ''): string | null {
  const origin = apiRequestOrigin(base, pageOrigin);
  if (isLoopbackOrigin(origin))
    return null;
  if (remoteApiOptIn() && origin !== null && remoteApiOriginAllowlist().includes(origin))
    return null;
  const where = origin ?? `an origin that could not be resolved from ${JSON.stringify(base)}`;
  return `This build would send the owner credential to ${where}, which is not this machine. `
    + 'Point NEXT_PUBLIC_API_BASE_URL at 127.0.0.1 or localhost. A remote origin is possible only by '
    + 'naming it exactly: set NEXT_PUBLIC_ALLOW_REMOTE_API=1 AND list that origin in '
    + 'NEXT_PUBLIC_API_ORIGIN_ALLOWLIST, e.g. NEXT_PUBLIC_API_ORIGIN_ALLOWLIST=https://staging.example.';
}
/** The page's own origin, which is what an empty API base resolves against. Empty when there is no
 *  page — a server render — and the resolution then fails closed. */
function pageOrigin(): string {
  return typeof window === 'undefined' ? '' : window.location.origin;
}
export function createLiveClient(base = process.env.NEXT_PUBLIC_API_BASE_URL ?? '',
                                from: string = pageOrigin()): Api & DecisionClient {
  async function request<T>(path: string, body?: unknown, method = body === undefined ? 'GET' : 'POST'): Promise<T> {
    const refusal = transportRefusal(base, from);
    if (refusal)
      throw new ApiError(0, refusal, 'insecure_transport');
    const token = ownerToken();
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (token)
      headers.Authorization = `Bearer ${token}`;
    const response = await fetch(`${base}/v2${path}`, { method, credentials: 'same-origin', cache: 'no-store', headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    if (!response.ok) {
      const error = await response.json().catch(() => ({})) as {
        detail?: string;
        code?: string;
      };
      throw new ApiError(response.status, error.detail ?? `Request failed (${response.status})`, error.code);
    }
    if (response.status === 204)
      return undefined as T;
    return response.json() as Promise<T>;
  }
  const path = (id: string) => `/changes/${encodeURIComponent(id)}`;
  return {
    limits: () => request('/limits'),
    setLimits: mode => request('/limits', { mode }),
    teach: rule => request('/teach', { rule }),
    skills: () => request('/skills'),
    learn: id => request(`${path(id)}/learn`, {}),
    learning: id => request(`/learning/${encodeURIComponent(id)}`),
    installSkill: async (id, identity) => { await request(`/learning/${encodeURIComponent(id)}/approve`, { identity }); },
    rejectLearning: async id => { await request(`/learning/${encodeURIComponent(id)}/reject`, {}); },
    revokeSkill: async name => { await request(`/skills/${encodeURIComponent(name)}/revoke`, {}); },
    // No `preview()` here any more. The live client used to answer it with the SAMPLE's synthetic
    // proposal — a live transport returning fixture data it never fetched, under a method name that
    // reads as "show me this server's state". It was one sentence away from a claim about a backend
    // that had not been asked. The sample adapter still has it (`SamplePreview`); a caller that wants
    // the illustration asks the sample for it, by name.
    // `mode` is the BACKEND's answer, passed through rather than overwritten with 'live'. The one
    // thing it decides is whether the sample's own controls are shown, and the backend is the only
    // party that knows: a live client pointed at `privatemesh serve --v2 --sample` is the judges'
    // path, and the second half of the claim — the second time, it does not ask again — is only
    // reachable from a button that a client-side 'live' would have hidden. The API base URL and
    // `authenticated_owner` still describe the transport, so nothing here is a claim that this
    // build is the sample.
    session: async () => { const session = await request<Session>('/session'); return { ...session, mode: session.mode === 'sample' ? 'sample' : 'live', authenticated_owner: session.authenticated_owner === true }; },
    today: (date) => request(`/today${date ? `?date=${encodeURIComponent(date)}` : ''}`),
    prepare: (id) => request(`${path(id)}/prepare`, {}), change: (id) => request(path(id)),
    leave: (id) => request(`${path(id)}/leave`, {}), edit: (id, edit) => request(`${path(id)}/edit`, edit),
    approve: (id, version) => request(`${path(id)}/approve`, { version }), outcome: (id) => request(`${path(id)}/outcome`), why: (id) => request(`${path(id)}/why`),
    decisions: (id) => request(`${path(id)}/decisions`),
    // The owner's receipt: every prompt this change sent, verbatim, and the categories that never
    // left. Owner-token-gated server-side; a build that cannot send the header gets a 403, which is
    // the correct outcome and is rendered as "not available", never as "nothing was sent".
    modelView: (id) => request(`${path(id)}/model-view`),
    agreements: async () => (await request<{ agreements: import('./v2-types').Agreement[] }>('/agreements')).agreements,
    // POST, and read-only on purpose: it re-reads the calendar at the moment the owner asks. The
    // method is what the contract specifies; it is not a mutation of anything the owner agreed to.
    checkAgreements: async () => (await request<{ agreements: import('./v2-types').Agreement[] }>('/agreements/check', {})).agreements,
    // ---- the caregiver -------------------------------------------------------------------
    // Owner-token-gated on every verb, the GET included: the answer names the people in the
    // owner's parent's life. A build with no token gets a 403, which the screen renders as "not
    // available from this server" and never as an empty circle — a missing list must not read as
    // an absent restriction, which is the same rule the gate itself follows.
    careCircle: async () => (await request<{ members: import('./v2-types').CareMember[] }>('/care-circle')).members,
    // No `added_by` in the body. The server writes `"owner"`; sending one would make the single
    // field that records who enrolled somebody a field this client chooses.
    addCareMember: value => request('/care-circle', { name: value.name, email: value.email, may_receive: value.may_receive }),
    removeCareMember: async id => { await request(`/care-circle/${encodeURIComponent(id)}`, undefined, 'DELETE'); },
    // The value goes UP on the POST and is never served back by anything. There is no client
    // method that reads one because there is no route that returns one.
    protectedFacts: () => request('/protected-facts'),
    addProtectedFact: value => request('/protected-facts', value),
    removeProtectedFact: async id => { await request(`/protected-facts/${encodeURIComponent(id)}`, undefined, 'DELETE'); },
    preferences: () => request('/preferences'), savePreference: (value) => request('/preferences', value), updatePreference: (id, value) => request(`/preferences/${encodeURIComponent(id)}`, value, 'PATCH'), deletePreference: (id) => request(`/preferences/${encodeURIComponent(id)}`, undefined, 'DELETE'),
    plans: () => request('/plans'), routine: (paused) => request('/routine', { paused }), setupBoundary: (value) => request('/boundary', value), connect: (service) => request(`/connections/${service}`, {}),
    // The sample's demo control, and the ONE call that is not under `/v2`: the contract is explicit
    // that `nextSampleChange` is a client-owned fixture rather than an endpoint, so it lives beside
    // the contract instead of inside it. The backend answers 404 when it is not running as the
    // sample, which is why the button that calls this is gated on the SESSION's mode, not the
    // client's — in live mode against a live backend there is nothing to open and nothing to fake.
    nextSampleChange: async () => {
      const response = await fetch(`${base}/sample/next-change`, { method: 'POST', credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) {
        const error = await response.json().catch(() => ({})) as { detail?: string; code?: string };
        throw new ApiError(response.status, error.detail ?? `Request failed (${response.status})`, error.code);
      }
      return response.json() as Promise<import('./v2-types').Change>;
    },
  };
}
export const api: Api & DecisionClient = apiMode === 'live' ? createLiveClient() : createSampleAdapter();
