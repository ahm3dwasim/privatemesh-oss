/**
 * Read the BUILT artifact and assert the owner credential is not in it.
 *
 * `NEXT_PUBLIC_*` is inlined into the bundle at build time and served to anyone who loads the page.
 * A token that arrives that way is not a token, it is a published credential: every visitor is the
 * owner and the gate is decoration. So the claim this script checks is not "the client does not send
 * the token" — that is a claim about code someone can edit — it is "the credential cannot be IN the
 * artifact even when the build environment contains it".
 *
 * That is why it is run against a build made WITH the canary set. `npm run verify` exports
 * `NEXT_PUBLIC_OWNER_TOKEN` to the canary value and then builds. If any later change reads that
 * variable again, next inlines the canary and this fails with the file and the offset, rather than
 * the mistake surviving to a deployment where nobody is looking at the bundle.
 *
 * The credential itself is never passed in: the canary is a synthetic string, and this script never
 * has, reads or needs a real token.
 */
import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs';
import { join, relative } from 'node:path';

export const CANARY_ENV = 'NEXT_PUBLIC_OWNER_TOKEN';
/** Also accepted from the environment, so the value lives in ONE place: the `verify` script. */
const CANARY = process.env.PM_OWNER_TOKEN_CANARY || 'pm-owner-token-canary-2f9c';
/** The name of the variable, not its value. Minified client output strips comments, so finding this
 *  identifier in the shipped JavaScript means something in `lib/`, `components/` or `app/` reads it
 *  again — which is the regression this half catches even when the canary happens to be unset. */
const CANARY_NAME = CANARY_ENV;

const ROOTS = ['.next/static', '.next/server', 'out'];
/** Build caches hold compiler state, not shipped bytes, and are large enough to make this slow
 *  enough that somebody would eventually skip it. The published artifact is what is read. */
const SKIP = new Set(['cache', 'node_modules']);

function walk(dir, files = []) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  }
  catch {
    return files;
  }
  for (const entry of entries) {
    if (SKIP.has(entry.name))
      continue;
    const path = join(dir, entry.name);
    if (entry.isDirectory())
      walk(path, files);
    else if (entry.isFile() && statSync(path).size > 0)
      files.push(path);
  }
  return files;
}

export function scan(needles, roots = ROOTS) {
  const hits = [];
  const scanned = [];
  for (const root of roots) {
    if (!existsSync(root))
      continue;
    for (const file of walk(root)) {
      scanned.push(file);
      const bytes = readFileSync(file);
      for (const needle of needles) {
        const at = bytes.indexOf(needle);
        if (at >= 0)
          hits.push({ file, needle, at });
      }
    }
  }
  return { hits, scanned };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  if (!existsSync('.next')) {
    console.error('check-bundle: no .next directory. Run `npm run build` first; this reads the artifact, it does not make one.');
    process.exit(1);
  }
  const { hits, scanned } = scan([CANARY, CANARY_NAME]);
  if (hits.length) {
    console.error('check-bundle: the owner credential reached the built artifact.\n');
    for (const hit of hits)
      console.error(`  ${relative('.', hit.file)} contains ${JSON.stringify(hit.needle)} at byte ${hit.at}`);
    console.error('\nNEXT_PUBLIC_* is inlined at build time and served to every visitor. The owner token must');
    console.error('be entered at runtime (setOwnerToken in lib/api.ts) or read server-side, never bundled.');
    process.exit(1);
  }
  const roots = ROOTS.filter(existsSync);
  console.log(`check-bundle: ${scanned.length} built file(s) under ${roots.join(', ')} carry neither the canary nor the variable name.`);
}
