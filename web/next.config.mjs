import { assertStaticExportIsSample } from './scripts/build-guards.mjs';

// `PM_STATIC_EXPORT=1` builds the judges' sample as plain static files (no server, no model, no
// provider): the sample runs entirely in the browser, so a static host is the whole deployment.
//
// The guard runs HERE, at config load, because this is the last moment before the artifact exists
// and the only moment where refusing still costs nothing. See `scripts/build-guards.mjs` for why a
// live static export is not a smaller deployment but a different product — one with no owner gate
// in it at all. `npm run verify` runs the same function's self-test before every build.
assertStaticExportIsSample();

/** @type {import('next').NextConfig} */
export default { reactStrictMode: true, ...(process.env.PM_STATIC_EXPORT === '1' ? { output: 'export' } : {}) };
