import type { Decision } from './v2-types';

/**
 * `Decision` was spelled out here as well as in `./v2-types`, "kept local while the shared v2
 * contract is updated by the API lane". Two spellings of one API row is the arrangement where the
 * server's vocabulary grows in one of them and the other keeps accepting the old set — which is what
 * happened: the backend split a losing candidate slot out of `refused` into `discarded`, and this
 * copy still said three values, so nothing in the UI would have been asked to handle the fourth.
 * The contract type is the only spelling now.
 */
export type { Decision };

/** What a caller needs from a client to read a change's decision log. Narrow on purpose: the
 *  components that render the spine take this, not the whole `Api`. */
export type DecisionClient = { decisions(id: string): Promise<Decision[]> };
