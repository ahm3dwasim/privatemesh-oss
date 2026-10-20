/**
 * The static-export guard, proved against the REAL build rather than against a description of it.
 *
 * `build-guards.mjs --self-test` calls `assertStaticExportIsSample` with four hand-written
 * environments and checks what it returns. That proves the FUNCTION, and the sixth review's
 * finding is that it proves nothing about the BUILD: the guard could be correct and never wired
 * into `next.config.mjs`, or wired in after the config it is supposed to protect, and every
 * self-test case would still pass. The only artifact that settles it is a build that has to fail.
 *
 * So this runs one. `PM_STATIC_EXPORT=1 NEXT_PUBLIC_API_MODE=live` is the exact combination the
 * guard exists for — a live app shipped as plain files, with the whole backend half missing — and
 * the assertion is that `next build` exits non-zero AND says why. Both halves matter: a build that
 * failed for an unrelated reason (a typo in this file, a missing dependency) would otherwise be
 * reported green for the same reason a real refusal would.
 *
 * It runs BEFORE the real build in `npm run verify`, so if the guard ever stops firing, the failure
 * is a red line in the log rather than a live artifact served from a static host.
 */
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

//: The sentence `BuildRefusal` prints, and the name of the error it is thrown as. Matched on both,
//: because a build can fail loudly and still have failed to guard anything.
const REFUSAL = 'static files, but NEXT_PUBLIC_API_MODE=live';
const ORIGIN = 'BuildRefusal';

function fail(lines) {
  console.error(lines.join('\n'));
  process.exit(1);
}

const build = spawnSync('npx', ['--no-install', 'next', 'build'], {
  cwd: web,
  encoding: 'utf8',
  env: { ...process.env, PM_STATIC_EXPORT: '1', NEXT_PUBLIC_API_MODE: 'live' },
});

const output = `${build.stdout ?? ''}${build.stderr ?? ''}`;

if (build.error)
  fail([`✗ the probe build could not be started: ${build.error.message}`]);

if (build.status === 0) {
  fail([
    '✗ PM_STATIC_EXPORT=1 NEXT_PUBLIC_API_MODE=live BUILT. It must not.',
    '  The static-export guard is not protecting the build, and the artifact it just wrote is a',
    '  live client with no owner gate, no decision log and no /v2 routes behind it.',
    `  ${web}/.next now holds it — rebuild before serving anything from this directory.`,
  ]);
}

if (!output.includes(ORIGIN) || !output.includes(REFUSAL)) {
  fail([
    `✗ the build failed, but not because of the static-export guard (expected a ${ORIGIN} naming`,
    `  "${REFUSAL}"). A build that fails for another reason is not evidence that this one refuses.`,
    '---',
    output.trim().split('\n').slice(-12).join('\n'),
  ]);
}

console.log('✓ PM_STATIC_EXPORT=1 with NEXT_PUBLIC_API_MODE=live was refused by the real build');
