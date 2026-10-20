import type { Change, Decision, ModelView } from './v2-types';

/**
 * The public demo (`docs/API_V2.md` § "The public demo"): a real model, simulated providers, hard
 * caps, no credential of the owner's anywhere in the process. Typed separately from `Api` in
 * `v2-types.ts` because this is a different contract — session-based, unauthenticated, four
 * routes — not a fourth `mode` bolted onto the owner's own surface.
 */

export interface DemoInboxMessage {
  message_id: string;
  thread_id: string;
  sender: string;
  subject: string;
  body_text: string;
  received_at: string;
}

export interface DemoBudget {
  calls_left: number;
  degraded: { reason: 'calls' | 'rate' | 'session' | 'global'; detail: string } | null;
}

export interface DemoLedgerTotals {
  total_usd: number;
  today_usd: number;
  cap_total_usd: number;
  cap_daily_usd: number;
}

export interface DemoSpend {
  session?: DemoLedgerTotals;
  demo?: DemoLedgerTotals;
}

export interface DemoCaps {
  session_usd: number;
  session_calls: number;
  rate_requests: number;
  rate_window_s: number;
  total_usd: number;
  daily_usd: number;
  session_ttl_s: number;
}

/** `POST /v2/demo/session` and `GET /v2/demo/session/{id}`. */
export interface DemoSessionState {
  session_id: string;
  mode: 'demo';
  real: string[];
  simulated: string[];
  notice: string;
  budget: DemoBudget;
  caps: DemoCaps;
  spend: DemoSpend;
  inbox: DemoInboxMessage[];
}

/** Which model answered, in the caller's own words rather than the caller's guess from
 *  `compromised`: `label` is the sentence a screen may show verbatim, and `calls`/`cost_usd` are
 *  pinned at `0` on the scripted run because it never reserves against a ledger (see
 *  `docs/API_V2.md` § "The compromised run"). */
export interface DemoResultModel {
  kind: 'real' | 'scripted';
  label?: string;
  calls?: number;
  cost_usd?: number;
}

/** `POST /v2/demo/session/{id}/email`, and its `/email/compromised` sibling — same shape, plus
 *  `compromised` and `attempted` on the scripted run (see `docs/API_V2.md` § "The compromised
 *  run"). `compromised` is absent, not `false`, on a real-model result: the two routes answer
 *  different questions, and a page that had to compare `=== false` to tell them apart is a page
 *  that got the two confused once already. */
export interface DemoResult {
  session_id: string;
  change_id: string;
  budget: DemoBudget;
  spend: DemoSpend;
  change: Change | null;
  model_view: ModelView | null;
  decisions: Decision[];
  prepared?: boolean;
  prepare_refused?: { code: string; detail: string } | null;
  compromised?: boolean;
  attempted?: string[];
  model?: DemoResultModel;
}
