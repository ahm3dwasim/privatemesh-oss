/**
 * The one guard that has to run at BUILD time, kept as a plain function so that the build and the
 * check proving the guard works run the same code rather than two descriptions of it.
 *
 * Why this one cannot be a runtime check: a static export is files. There is no process to refuse a
 * request, no place to hold a token, and no server to enforce anything. Every guard this product
 * has — the owner gate on every private route, the decision log, the boundary, the single approved
 * effect — lives in the backend. `NEXT_PUBLIC_API_MODE=live` with `PM_STATIC_EXPORT=1` therefore
 * ships the client half of a two-half system as if it were the whole thing, and the half it drops
 * is the half that does the refusing. The failure is silent and the artifact looks fine, which is
 * exactly why it is a build failure and not a warning.
 */
export const STATIC_EXPORT_ENV = 'PM_STATIC_EXPORT';
export const API_MODE_ENV = 'NEXT_PUBLIC_API_MODE';

/** Thrown rather than a bare `Error` so the self-test can tell a guard firing from a typo in here. */
export class BuildRefusal extends Error {
  constructor(message) {
    super(message);
    this.name = 'BuildRefusal';
  }
}

/**
 * Refuse to build a live app as static files. `sample` is the only mode a static export can be:
 * the sample runs entirely in the browser, which is why a static host is a whole deployment for it.
 */
export function assertStaticExportIsSample(env = process.env) {
  if (env[STATIC_EXPORT_ENV] !== '1')
    return;
  const mode = env[API_MODE_ENV] || 'sample';
  if (mode === 'sample')
    return;
  throw new BuildRefusal(
    `${STATIC_EXPORT_ENV}=1 builds this app as plain static files, but ${API_MODE_ENV}=${mode}.\n`
    + 'A static export has no server, so it has no owner gate, no decision log and no /v2 routes:\n'
    + 'the half that does the refusing would not be shipped at all. The artifact would look correct\n'
    + `and answer every request, which is the failure mode this refusal exists to prevent.\n`
    + `Build with ${API_MODE_ENV}=sample for the judges' static sample, or drop `
    + `${STATIC_EXPORT_ENV}=1 and run the live app as a server (npm run build && npm start).`,
  );
}

/** The guard, demonstrated. Run from `npm run verify` before the build it protects. */
export function selfTest(log = console.log) {
  const cases = [
    { name: 'static export + sample builds', env: { [STATIC_EXPORT_ENV]: '1', [API_MODE_ENV]: 'sample' }, refuses: false },
    { name: 'static export + no mode builds (sample is the default)', env: { [STATIC_EXPORT_ENV]: '1' }, refuses: false },
    { name: 'no static export + live builds (it is a server)', env: { [API_MODE_ENV]: 'live' }, refuses: false },
    { name: 'static export + live is REFUSED', env: { [STATIC_EXPORT_ENV]: '1', [API_MODE_ENV]: 'live' }, refuses: true },
  ];
  let failed = 0;
  for (const { name, env, refuses } of cases) {
    let refusal = null;
    try {
      assertStaticExportIsSample(env);
    }
    catch (error) {
      refusal = error;
    }
    if (refuses && !(refusal instanceof BuildRefusal)) {
      log(`✗ ${name} — the build would have succeeded`);
      failed++;
      continue;
    }
    if (!refuses && refusal) {
      log(`✗ ${name} — ${refusal.message.split('\n')[0]}`);
      failed++;
      continue;
    }
    log(`✓ ${name}${refusal ? ` — ${refusal.message.split('\n')[0]}` : ''}`);
  }
  if (failed) {
    log(`\n${failed} build-guard case(s) did not behave. The static-export guard is not protecting the build.`);
    return 1;
  }
  return 0;
}

if (process.argv[1] && process.argv[1].endsWith('build-guards.mjs') && process.argv.includes('--self-test'))
  process.exit(selfTest());
