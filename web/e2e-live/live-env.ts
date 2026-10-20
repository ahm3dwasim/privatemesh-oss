// The live suite's addresses, in ONE place: the Playwright config starts the servers on these
// ports and the spec calls the API on them directly, and a spec that guessed a different port than
// the config launched would fail as "connection refused" rather than as the mismatch it is.
//
// The ports are environment-overridable because the backend keeps the sample's state in memory:
// the whole journey runs once per viewport, and the second run needs its own backend rather than
// the finished one the first run left behind. The npm script moves both pairs together.
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export const API_PORT = Number(process.env.PM_LIVE_API_PORT ?? 8831);
export const WEB_PORT = Number(process.env.PM_LIVE_WEB_PORT ?? 3131);

export const API = `http://127.0.0.1:${API_PORT}`;
export const WEB = `http://127.0.0.1:${WEB_PORT}`;

// The third server: a LIVE-mode backend, which is the only process in this suite where the owner
// gate is load-bearing. `API` above is the sample, and the sample is open on purpose — a suite that
// asked it whether a private route refuses would be asking a server whose answer is documented to
// be no. So the route-gate assertions get their own backend, and it is live.
//
// It binds neither 8765 nor 3201 (the owner's own port and the desktop pair), because a test that
// binds a port someone else is serving is not a test, it is an outage.
export const GATE_PORT = Number(process.env.PM_LIVE_GATE_PORT ?? 8833);
export const GATE = `http://127.0.0.1:${GATE_PORT}`;

// Where that backend minted the owner token, and where its stores live. Both are outside the repo
// on purpose: a credential — even a throwaway one — does not belong in a working tree, and a store
// that a previous run left behind must never be the reason a later run agrees with it.
export const GATE_TOKEN_FILE = join(tmpdir(), `pm-e2e-owner-${GATE_PORT}.token`);
export const GATE_DATA_DIR = join(tmpdir(), `pm-e2e-live-${GATE_PORT}`);
