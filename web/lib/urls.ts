/**
 * The two questions this app asks about a URL before it will do anything with it. They live here,
 * rather than at the call sites, because a call site is exactly where a check gets forgotten — and
 * both of these were forgotten at five of them.
 *
 * `safeHttpUrl` — every URL this UI renders into an `href` comes from the server: a venue's evidence
 * link, a provider record, a thread link. The server is trusted, but "trusted" is not the same as
 * "checked", and the failure is not a broken link. An `href` is a place a scheme executes: a
 * `javascript:` URL runs script on click, with the app's own origin, inside the page that is showing
 * the owner their private data. A scheme allowlist of exactly two entries is the whole fix, and it
 * costs nothing when the value is what it should be.
 *
 * `apiRequestOrigin` and `isLoopbackOrigin` — the live client sends the owner's credential. Where it
 * sends it is therefore a security decision, not a configuration one: an origin that is not this
 * machine puts the owner token on a network the owner does not control. The two are separate
 * functions on purpose, and the pair is what makes an empty base safe to reason about: the first
 * resolves `''` against the PAGE's origin the way the browser will, and only the second judges the
 * result. See `createLiveClient` in `./api`.
 */

/** The two schemes an `href` may ever carry. Both are navigation to a document; nothing else is. */
const NAVIGABLE = new Set(['http:', 'https:']);

const LOOPBACK_HOSTNAMES = new Set(['localhost', '127.0.0.1', '::1', '0.0.0.0']);

/**
 * The URL, if it is an absolute http(s) one, otherwise `null`. Callers render the anchor only for a
 * non-null result, so a rejected URL is a link that is not drawn — never a link drawn with a
 * sanitised-looking but still executable value in it.
 *
 * Relative and protocol-relative values are rejected too. `/path` cannot be checked against an
 * allowlist without knowing the origin, and `//host/path` resolves to a DIFFERENT origin than the
 * one it appears to sit on — the two shapes that look safest are the two a naive check waves through.
 * The server sends absolute URLs, and a caller that gets `null` has a stated fallback for it.
 */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value)
    return null;
  let url: URL;
  try {
    url = new URL(value);
  }
  catch {
    return null;
  }
  return NAVIGABLE.has(url.protocol) ? url.href : null;
}

/**
 * The origin a request built from `base` actually goes to, or `null` when it cannot be determined.
 *
 * `''` is the case this exists for, and the version it replaces got it backwards. An empty base was
 * read as "same origin, therefore this machine" — but a RELATIVE URL resolves against the PAGE's
 * origin, and the page is wherever the build was served from. `https://app.example` with no API base
 * sends `Authorization: Bearer <owner token>` to `https://app.example/v2/...`, which is a stranger's
 * server, reached by the one configuration that looked safest. So `''` is resolved against
 * `pageOrigin` — the same answer the browser's own URL resolution will reach — and then judged like
 * any other origin. `pageOrigin` is `window.location.origin` in the browser; in a context with no
 * page (server render, a test harness that supplies nothing) it is `''`, the resolution fails, and
 * the caller refuses. Fail-closed is the right direction here: "I could not tell where this goes" is
 * not a reason to send a credential.
 */
export function apiRequestOrigin(base: string, pageOrigin: string): string | null {
  try {
    return new URL(base || pageOrigin).origin;
  }
  catch {
    return null;
  }
}

/** Whether a resolved ORIGIN is this machine. Takes an origin, never a base, on purpose: a base of
 *  `''` has no host to inspect, and answering `true` for it is the defect above. */
export function isLoopbackOrigin(origin: string | null): boolean {
  if (!origin)
    return false;
  let url: URL;
  try {
    url = new URL(origin);
  }
  catch {
    return false;
  }
  const host = url.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  return LOOPBACK_HOSTNAMES.has(host) || host.endsWith('.localhost');
}

/**
 * The origins a build may send the owner credential to when the opt-in is set. An EXACT match on
 * the resolved origin — scheme, host and port, after `URL` has normalised them — and never a
 * pattern, a suffix or a wildcard. The version this replaces accepted `NEXT_PUBLIC_ALLOW_REMOTE_API=1`
 * on its own, which is an opt-in to "anywhere": a build pointed at `https://staging.example` with
 * the flag set forwarded the owner's token to whatever `NEXT_PUBLIC_API_BASE_URL` happened to say,
 * and the person who set the flag had approved an idea rather than an address.
 *
 * The value is a comma-separated list of origins, e.g.
 * `NEXT_PUBLIC_API_ORIGIN_ALLOWLIST=https://staging.example`. An entry that is not an origin at all
 * is kept as written and simply never matches, so a typo refuses instead of widening.
 */
export function remoteApiOriginAllowlist(): string[] {
  const raw = process.env.NEXT_PUBLIC_API_ORIGIN_ALLOWLIST ?? '';
  return raw.split(',').map(entry => entry.trim()).filter(Boolean).map((entry) => {
    try {
      return new URL(entry).origin;
    }
    catch {
      return entry;
    }
  });
}
