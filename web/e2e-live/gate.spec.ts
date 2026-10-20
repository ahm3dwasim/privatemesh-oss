import { readFileSync } from 'node:fs';

import { expect, test, type APIRequestContext } from '@playwright/test';

import { GATE, GATE_TOKEN_FILE } from './live-env';

/**
 * The owner gate, against a REAL live-mode server, over REAL HTTP.
 *
 * `journey.spec.ts` runs the judges' path against `serve --v2 --sample`, and sample mode is open on
 * purpose: a private route there answers a caller with no credential, by design, and a test that
 * asked it whether private routes refuse would be asking a server whose documented answer is no.
 * So this file gets its own backend — `e2e-live/live-backend.py`, a `build_live` wiring with stub
 * collaborators, `mode == "live"`, and a minted owner token. That is the only configuration in
 * which the gate is load-bearing, and the gate is what the fourth review's Critical was about.
 *
 * The route list is NOT written out here, and that is the point of the finding rather than a
 * flourish. The review found nine routes written without the check; a test that lists those nine
 * certifies today's file and says nothing about tomorrow's. This one reads the running server's own
 * `/openapi.json` and demands a decision about every path it finds — so a route added later is
 * covered the moment it exists, without anyone remembering to add it here.
 */

/** Routes that may answer a caller with no owner token. Mirrors `PUBLIC_V2_ROUTES` in
 *  `src/privatemesh/api_v2.py`, and is deliberately short: the assertion below fails on anything
 *  NOT named here, so widening the public surface cannot happen by accident in either file. */
const PUBLIC_V2_ROUTES = new Set(['POST /v2/playground/attack']);

/** The four codes the gate is allowed to refuse with. `owner_forbidden` is the default a new route
 *  inherits; the other two are the documented per-route codes `docs/API_V2.md` names and the web
 *  client branches on. Anything else — including a bare 500 — is not a refusal. */
const GATE_CODES = new Set(['owner_forbidden', 'approval_forbidden', 'decisions_forbidden']);

/** A path parameter has to be *something*; the gate refuses before any handler reads it, so the
 *  value never has to be meaningful. Proven, not assumed: a POST with a body that cannot satisfy
 *  its own schema still returns the gate's 403 rather than a 422. */
const PLACEHOLDER = 'route-param';

type Route = { method: string; path: string; url: string; label: string; body?: unknown;
  gated: boolean; public: boolean; requestGate: boolean };
type Json = Record<string, any>;

/**
 * A value of the right SHAPE for one schema, so a request reaches the HANDLER the gate wraps.
 *
 * This is not decoration; it is what stops the file from passing for the wrong reason. The gate is
 * applied around each route's handler, which means FastAPI validates the body first — so a POST
 * sent with `{}` where the body model requires a field answers 422, and a 422 is indistinguishable
 * from "the gate ran" to a test that only asserts "not 200". The value never has to be MEANINGFUL:
 * the gate refuses before the handler reads it. It only has to be well-typed enough to get there.
 */
function example(schema: Json | undefined, schemas: Json, depth = 0): unknown {
  if (!schema || depth > 6) return null;
  if (schema.$ref) return example(schemas[String(schema.$ref).split('/').pop()!], schemas, depth + 1);
  for (const key of ['anyOf', 'oneOf', 'allOf'] as const) {
    if (Array.isArray(schema[key]) && schema[key].length) {
      // `anyOf` includes `null` for every optional field; picking the first NON-null branch is what
      // makes an optional object arrive as an object rather than as a 422.
      const branch = schema[key].find((option: Json) => option.type !== 'null') ?? schema[key][0];
      return example(branch, schemas, depth + 1);
    }
  }
  if (Array.isArray(schema.enum) && schema.enum.length) return schema.enum[0];
  switch (schema.type) {
    case 'object': {
      const out: Json = {};
      const properties: Json = schema.properties ?? {};
      for (const name of schema.required ?? []) out[name] = example(properties[name], schemas, depth + 1);
      return out;
    }
    case 'array': return [];
    case 'integer': return 0;
    case 'number': return 0;
    case 'boolean': return false;
    default: return PLACEHOLDER;
  }
}

/** Every `/v2` route the running server SERVES, with a body its own schema will accept.
 *
 *  The list comes from `/e2e/routes` — the harness's view of the app's `routes` attribute — and not
 *  from `/openapi.json`, which is the sixth review's finding. The schema is a view of the routes,
 *  and a route with `include_in_schema=False`, or one registered straight on the app past the
 *  router builder, is not in it: the sweep would have been complete over a list that was missing
 *  exactly the kind of route worth sweeping. The SCHEMA is still what supplies each request body,
 *  because that is what makes a request reach the handler the gate wraps rather than stopping at a
 *  422 — see `example`.
 *
 *  Both flags come from the server with the list (`gated` is stamped on the handler by the gate
 *  itself, `public` is the gate's own identity test over its vetted-route registry), so the two
 *  assertions that use them are about the running process rather than about a copy of its tables
 *  kept in this file. */
async function liveRoutes(request: APIRequestContext): Promise<Route[]> {
  const listed = await request.get(`${GATE}/e2e/routes`);
  expect(listed.status(), 'the live backend must publish its own route table').toBe(200);
  const rows = (await listed.json()) as { method: string; path: string; gated: boolean;
    public: boolean; request_gate: boolean }[];
  expect(rows.length, 'the live backend served no routes').toBeGreaterThan(10);

  // The boundary first, before anything is concluded from the routes. Every assertion below reads
  // "this route refuses an anonymous caller" — and the eighth review's first High was that a server
  // can answer that way for every route at the moment it booted and still serve a route registered
  // afterwards. If this backend came up without the per-request gate, the rest of this file would be
  // certifying a startup scan. The server is asked rather than trusted: `build_app` is what installs
  // it, and the flag is read off the app's own middleware stack.
  for (const row of rows) {
    expect(row.request_gate, 'the live backend did not install the per-request gate').toBe(true);
  }

  const schemaResponse = await request.get(`${GATE}/openapi.json`);
  expect(schemaResponse.status(), 'the live backend must publish its schema').toBe(200);
  const schema = (await schemaResponse.json()) as Json;
  const schemas: Json = schema.components?.schemas ?? {};

  const routes: Route[] = rows.map((row) => {
    const operation = schema.paths?.[row.path]?.[row.method.toLowerCase()];
    const body = operation?.requestBody?.content?.['application/json']?.schema;
    return { method: row.method, path: row.path, gated: row.gated, public: row.public,
      requestGate: row.request_gate,
      url: `${GATE}${row.path.replace(/\{[^}]+\}/g, PLACEHOLDER)}`,
      label: `${row.method} ${row.path}`, ...(body ? { body: example(body, schemas) } : {}) };
  });

  // The listing is the APP's, so it can only be a superset of the schema's. Asserted rather than
  // assumed: if it is ever narrower, the sweep below is narrower than the schema it replaced and
  // the regression would be invisible.
  for (const path of Object.keys(schema.paths ?? {})) {
    if (!path.startsWith('/v2/')) continue;
    for (const method of Object.keys(schema.paths[path])) {
      const label = `${method.toUpperCase()} ${path}`;
      expect(routes.some(route => route.label === label), `${label} is served but not listed`)
        .toBe(true);
    }
  }
  return routes.sort((a, b) => a.label.localeCompare(b.label));
}

/** One request in the shape the METHOD allows, carrying the body its own schema asked for. */
function send(request: APIRequestContext, route: Route, headers: Record<string, string> = {}) {
  const options = { headers, failOnStatusCode: false };
  switch (route.method) {
    case 'GET': return request.get(route.url, options);
    case 'DELETE': return request.delete(route.url, options);
    case 'PATCH': return request.patch(route.url, { ...options, data: route.body ?? {} });
    case 'PUT': return request.put(route.url, { ...options, data: route.body ?? {} });
    default: return request.post(route.url, { ...options, data: route.body ?? {} });
  }
}

function token(): string {
  // Read, never guessed and never defaulted: the token the server wrote at startup is the only
  // thing that opens these routes, and a fallback here would let the positive half of this file
  // pass against a server that had not armed the gate at all.
  return readFileSync(GATE_TOKEN_FILE, 'utf8').trim();
}

test('every private v2 route refuses a caller who presents no owner token', async ({ request }) => {
  const routes = await liveRoutes(request);
  expect(routes.length, 'the live server published no v2 routes').toBeGreaterThan(10);

  const refused: string[] = [];
  const open: string[] = [];
  for (const route of routes) {
    // The invariant the Python startup guard enforces, asked of the running server: nothing is
    // served under `/v2` that is neither behind the gate nor named public. A route registered past
    // the router builder — `route_class_override`, `add_api_route`, a mount — is caught by the
    // per-request gate (`request_gate` above) and reaches the browser as a 403 below rather than as
    // a 200. This line is the startup half of that pair, and it is kept because a server that fails
    // it is misconfigured in a way the refusal would otherwise hide.
    expect(route.gated || route.public,
      `${route.label} is neither owner-gated nor declared public`).toBe(true);
    // And the spec's own copy of the allowlist still agrees with the server's. A mirror that
    // drifted would make the "open" half below a statement about this file rather than the server.
    // The server's side is now `_is_public` — an identity test over the routes the gated router
    // built — so agreement here also says the vetted route is the one being served, not a lookalike.
    expect(PUBLIC_V2_ROUTES.has(route.label), `${route.label} disagrees on being public`)
      .toBe(route.public);

    const response = await send(request, route);
    if (route.public) { open.push(route.label); continue; }
    // A refusal, not merely a failure: the status AND the code the client reads, because a 403
    // with no code is a refusal the UI cannot tell from any other.
    expect(response.status(), `${route.label} answered ${response.status()} without a token`).toBe(403);
    const body = (await response.json()) as { code?: string };
    expect(GATE_CODES.has(String(body.code)), `${route.label} refused with code ${body.code}`).toBe(true);
    refused.push(route.label);
  }

  // Both halves, so the assertions above cannot pass by the enumeration having quietly found
  // nothing. The second is the finding's own list: the nine routes that were written without the
  // check must be among those that now refuse.
  expect(open.sort()).toEqual([...PUBLIC_V2_ROUTES].sort());
  for (const path of ['/v2/session', '/v2/today', '/v2/plans', '/v2/preferences', '/v2/skills',
    '/v2/changes/{change_id}', '/v2/changes/{change_id}/decisions',
    '/v2/changes/{change_id}/outcome', '/v2/changes/{change_id}/why',
    '/v2/learning/{learning_id}', '/v2/connections/{service}']) {
    expect(refused.some(label => label.endsWith(` ${path}`)), `${path} is not gated`).toBe(true);
  }
});

test('the owner token is what opens them, and it is the only thing that does', async ({ request }) => {
  const routes = await liveRoutes(request);
  const headers = { Authorization: `Bearer ${token()}` };

  // The reads the live client makes when the owner opens it. Not all 45 routes: this backend's
  // providers are stubs that answer an empty account, so a route that RUNS something — `prepare`,
  // `approve`, `teach` — would fail here on the stub rather than on the gate, and a test that
  // demanded 200 from those would be asserting the quality of the fake. The mutation routes are
  // covered by the refusal half above, where the gate is the only thing being asked, and by the
  // Python suite, which drives them end to end against real fakes.
  const OPENS = ['GET /v2/session', 'GET /v2/today', 'GET /v2/plans', 'GET /v2/preferences',
    'GET /v2/skills', 'GET /v2/limits', 'GET /v2/agreements'];

  for (const label of OPENS) {
    const route = routes.find(candidate => candidate.label === label);
    expect(route, `${label} is not a route this server publishes`).toBeDefined();
    const response = await send(request, route!, headers);
    // A real answer, not merely the absence of a 403: a 403 is what a caller with the WRONG
    // credential gets, so a token that was never read would look identical to a token that was. A
    // 5xx is not an answer either — it would mean the gate opened onto a broken route.
    expect(response.status(), `${label} did not answer its owner`).toBe(200);
  }

  // And a wrong token is not a token. `owner_forbidden` here is what makes the 403s above a gate
  // rather than a route that is simply broken for everybody.
  const wrong = await send(request, routes.find(route => route.label === 'GET /v2/session')!,
    { Authorization: `Bearer ${'x'.repeat(43)}` });
  expect(wrong.status()).toBe(403);
  expect(((await wrong.json()) as { code: string }).code).toBe('owner_forbidden');
});

test('a browser that opens a private route directly is refused by the server', async ({ page }) => {
  // The same refusal through a real navigations stack — a browser, not a test client. The web
  // client cannot fake this one: the client is not what refuses, and the assertion is that the
  // bytes arriving in a page are the server's refusal.
  await page.goto(`${GATE}/v2/session`);
  const body = await page.locator('body').innerText();
  expect(body).toContain('owner_forbidden');
  // And the refusal never hands back what it was protecting.
  expect(body).not.toContain('authenticated_owner');
});
