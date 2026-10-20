export type Stage = 'detected' | 'reading_thread' | 'checking_availability' | 'checking_venues' | 'proposal_ready' | 'no_safe_fit' | 'approved' | 'executing' | 'done' | 'partial' | 'stopped';
export type Service = 'gmail' | 'calendar' | 'tavily' | 'nemotron';
export type SampleVariant = 'happy' | 'partial' | 'unknown' | 'stale' | 'no_safe_fit' | 'expired' | 'missing' | 'paused' | 'empty' | 'authorizing' | 'expired_connection' | 'long_wait' | 'limit_handoff';
export interface Connection {
  service: Service;
  state: 'disconnected' | 'authorizing' | 'connected' | 'expired';
  detail: string;
}
export interface Session {
  mode: 'sample' | 'live';
  authenticated_owner: boolean;
  owner_name: string;
  timezone: string;
  connections: Connection[];
  routine: {
    enabled: boolean;
    paused: boolean;
    last_checked_at: string | null;
  };
  boundary: {
    start: string;
    buffer_minutes: number;
    configured: boolean;
  };
}
export interface CalendarBlock {
  id: string;
  title: string;
  start: string;
  end: string;
  kind: 'event' | 'proposed' | 'confirmed';
}
export interface ProtectedBlock {
  id: string;
  start: string;
  end: string;
  buffer_minutes: number;
  label: 'Protected';
}
export interface ChangeSummary {
  id: string;
  from_name: string;
  from_address: string;
  received_at: string;
  excerpt: string;
  thread_url: string | null;
  stage: Stage;
}
export interface Today {
  date: string;
  timezone: string;
  events: CalendarBlock[];
  protected: ProtectedBlock[];
  changes: ChangeSummary[];
}
export interface Evidence {
  url: string | null;
  title: string;
  excerpt: string;
  retrieved_at: string;
  official: boolean;
  sample: boolean;
}
export interface Venue {
  name: string;
  address: string;
  evidence: Evidence;
  note: string;
}
export type Action = {
  kind: 'calendar_update';
  event_id: string;
  attendees: string[];
  diff: {
    start: [
      string,
      string
    ];
    end: [
      string,
      string
    ];
    location: [
      string,
      string
    ];
  };
  notifies_attendees: boolean;
} | {
  kind: 'email_reply';
  to: string[];
  subject: string;
  body: string;
} | CareDigestAction;

/**
 * One outbound family digest, as the review screen needs it.
 *
 * `docs/API_V2.md` says the digest "surfaces through the ordinary change shape, so the screen is
 * the ordinary review screen. The fields the lane needs are the ones already there — `change.id`,
 * the review display's fragments in order, and the recipients", and that `docs` deliberately adds
 * no new field for it. This action is that sentence typed: it is the change's outbound, carried in
 * the `actions` array beside the other two kinds, and every field on it is one the doc names.
 *
 * It is also THE WEB LANE'S PROPOSAL, and the doc invites exactly one: "there is no UI for
 * approving a specific artifact's disclosure — `approve_disclosure` exists and is tested, and the
 * route that would let an owner press it is the web lane's to propose." No route serves this shape
 * today. `lib/sample.ts` supplies it; a live server that does not is rendered with the ordinary
 * reply view and never with an invented digest.
 *
 * `artifact_id` is NOT trusted as the identity. The screen recomputes it from the bytes it is
 * about to show (`lib/care.ts`), because an id the server asserts is a label beside the content,
 * and the property the owner is being promised — approving is approving these bytes — is only
 * demonstrated by a name that moves when a word does.
 */
export interface CareDigestAction {
  kind: 'care_digest';
  to: string[];
  subject: string;
  day: string;
  /** The composer's own name for these bytes. Compared with the browser's recomputation; when the
   *  two disagree the screen says so and refuses to call the approval a match. */
  artifact_id: string;
  fragments: { source: string; text: string; record_id: string }[];
  /** Every protected fact whose value is IN this artifact, by label and kind — plus the exact
   *  substring, because the owner is approving those bytes and cannot approve what they cannot
   *  read. This is the one place in the product a protected value is ever rendered, and it is the
   *  exception `docs/API_V2.md` names: "unless the owner approved that exact artifact containing it
   *  and every recipient is a care-circle member". No route returns a value, so this field is only
   *  ever populated by a composer that already held one. */
  discloses: { label: string; kind: string; value: string; fragment: number }[];
}
/**
 * One option the plan did not take, and — when a learned skill is what took it off the table —
 * which skill decided that.
 *
 * `by` and `code` come from the Teach Mode contract: "A slot a taught skill removes appears in the
 * plan's `rejected` list like any other refusal, and the entry names the skill that decided it:
 * `by` is the skill's name, and `code` is the reason code the skill's own answer carried". They are
 * OPTIONAL because the trusted validator's own refusals (a closed café, a boundary conflict) carry
 * neither, and those are the majority. A screen that wants to show what a taught rule narrowed today
 * reads these rows and nothing else: absence of `by` is not evidence a skill was involved.
 */
export interface RejectedOption {
  name: string;
  reason: string;
  evidence: Evidence | null;
  by?: string;
  code?: string;
}
export interface Proposal {
  start: string;
  end: string;
  venue: Venue;
  venues: Venue[];
  rejected: RejectedOption[];
  reason_sentence: string;
  before: CalendarBlock[];
  after: CalendarBlock[];
  protected: ProtectedBlock;
  actions: Action[];
  privacy_line: string;
  expires_at: string;
}
export interface Overreach { skill: string; attempted: string; blocked_by: 'runtime' | 'manifest'; detail: string }
export type LimitsMode = 'ask_always' | 'within_limits' | 'off';
export interface Limits { mode: LimitsMode; clauses: { id: string; text: string; cannot: string[] }[]; updated_at: string | null }
export interface LearningExample { input: string; kept: boolean; why: string }
export interface SkillSummary { name: string; purpose_plain: string; version: string | number; identity: string; state: 'active' | 'revoked'; learned_at: string; last_used_at: string | null; uses: number; rule: string | null }
export interface Learning {
  id: string; change_id: string; skill_name: string; purpose_plain: string;
  stage: 'generating' | 'testing' | 'reviewing' | 'revising' | 'ready_for_review' | 'failed';
  // `version` is a string | number because the contract does not type it and the two producers
  // name it differently: the backend versions a skill as semver ('1.0.1', the patch that the
  // revision bumped) and the sample fixture counts attempts (1, 2). Both render as `v{version}`.
  attempts: { version: string | number; tests: { passed: number; failed: number }; held_out: { passed: number; failed: number }; violations: { kind: string; detail: string }[]; review: { verdict: 'approve' | 'reject'; reasons: string[]; reviewer_model: string } }[];
  rule: string | null;
  examples: LearningExample[];
  manifest: { rule?: string | null; examples?: LearningExample[]; reads: string[]; proposes: string[]; never: string[]; expires: string; identity: string } | null;
  code_url: string | null;
}
export interface Change {
  executed_by: 'owner' | 'limit' | null;
  learning_id?: string;
  skill_used?: Pick<SkillSummary, 'name' | 'identity' | 'learned_at'>;
  id: string;
  version: number;
  stage: Stage;
  request_excerpt: string;
  proposal: Proposal | null;
  blocked_reason: string | null;
  /** How many requests this change made that the owner's rules stopped. Counts both `refused`
   *  and `withheld`: a `withheld` is a refusal of part of the change, and from the owner's seat
   *  that is the same event. It rides here rather than on the decisions list because the day view
   *  shows it before anybody opens the spine. */
  blocked_attempts: number;
  updated_at: string;
}
/**
 * One check the engine made, in the words the owner reads. The array from
 * `GET /v2/changes/{id}/decisions` is ordered by `seq` and is the spine the UI renders.
 *
 * `verdict` is four-valued, and two of the four are the reason it is not a boolean:
 *  - `allowed`   — the check passed and the thing proceeded.
 *  - `refused`   — the check failed and the thing did not proceed. Nothing happened.
 *  - `discarded` — the check failed for THIS candidate and the change proceeded on another one. A
 *    sender who offers 15:00 and 16:00 gets one slot taken and one left; the left one was not
 *    stopped, it was not chosen. Rendering it as `refused` put "Unsafe part blocked" over a plan
 *    that had simply picked the other time — an ordinary choice reported to the owner as a security
 *    event. It is also why `blocked_attempts` does not count it.
 *  - `withheld`  — the thing proceeded, but not with everything it asked for. The standing case is
 *    the protected commitment: the planner may know a window is unavailable and may not know why.
 *
 * The three words a UI may say are not these four. `refused` and `withheld` are the two that mean
 * something was STOPPED; `discarded` means the plan moved on without it. Folding them together is
 * the mistake this vocabulary exists to make impossible.
 *
 * `untrusted` is true when the thing being checked came from the email, the model or a learned
 * skill — content this system did not author and must not obey. The UI should not infer it from
 * `source`; the server derives it so a call site cannot forget.
 *
 * `detail` never contains protected text: it is run through the privacy gate on the way into the
 * log, and reads `"detail withheld: it did not pass the privacy gate"` when it does not survive.
 */
export interface Decision {
  seq: number;
  at: string;
  stage: 'read' | 'model' | 'search' | 'validate' | 'approve' | 'execute' | 'send';
  source: 'email' | 'model' | 'skill' | 'owner' | 'system';
  untrusted: boolean;
  subject: string;
  verdict: 'allowed' | 'refused' | 'withheld' | 'discarded';
  rule: string;
  detail: string | null;
}

/**
 * One model call, as `GET /v2/changes/{id}/model-view` reports it — built from the engine's own
 * audit rows, not from a second log.
 *
 * `prompt_sent` is the gated payload VERBATIM: the messages array the transport received, after
 * `egress_sanitize`. That is what makes the receipt evidence rather than a claim about intent, and
 * it is why the route is owner-token-gated. It is `null` — never `[]` — for a call the privacy gate
 * refused: nothing was sent, and an empty prompt would read as a call that sent nothing.
 *
 * `cost_usd` is `null` for a model with no published price, never `0.0`. `spend.py` refuses an
 * unknown price rather than calling it free; the sample's models are unpriced on purpose, so the
 * sample's model-view says "unknown" rather than "free".
 */
export interface ModelCall {
  phase: string;
  tier: string;
  model: string;
  prompt_sent: unknown[] | null;
  withheld: { field: string; reason: string }[];
  tokens: { prompt: number; completion: number; total: number };
  cost_usd: number | null;
}
/**
 * `GET /v2/changes/{id}/model-view` — what the model saw, and what it did not.
 *
 * `kept_on_machine.categories` NAMES the categories that never left and never lists their values:
 * the protected commitment's `title`, `location`, `reason` and `attendees`, plus `other_events` (the
 * day's other meetings). A screen that proved the title was safe by printing it would be the leak it
 * exists to show missing — the same rule `Why.kept_private` follows.
 *
 * `counts.other_events` is `number | null`, and the null is the point: it means the day was never
 * read, which is a different fact from `0` ("read, and it held nothing else"). Render them
 * differently — a `0` is evidence of non-disclosure and a `null` is a gap in the evidence.
 *
 * Owner-token-gated exactly as `/decisions` is, and 404 for an unknown change for the same reason:
 * "this change sent nothing" and "there is no such change" are different states. In the SAMPLE the
 * route is open (no token exists, the data is synthetic), and every `prompt_sent` is put through the
 * privacy gate before it is returned, so a call whose recorded prompt carried a protected value
 * comes back with `prompt_sent: null` and a `withheld` row saying which field it was.
 */
export interface ModelView {
  change_id: string;
  calls: ModelCall[];
  kept_on_machine: { categories: string[]; counts: Record<string, number | null> };
}
/**
 * `POST /v2/playground/attack` — the judge writes an email, the real sample engine answers it.
 *
 * **Sample only: `404` in live mode**, and not owner-gated — a judge has to be able to run it
 * without the owner token, and what it reaches is a world built fresh for the request (sample ports,
 * a mock transport, no key, no network). The caps are what stand in: 8 KB of request body, 2,000
 * characters of email, 1 s of wall clock, 10 attacks a minute per caller.
 *
 * `verdict` comes from `redteam.evaluate`, the same function the corpus is scored with, so a `held`
 * here means what it means there. `attempted` is what the COMPLIANT model tried — reported even on
 * a `held`, because a refusal of something nobody attempted proves nothing.
 *
 * The third verdict is not a softer `held`. `not_attempted` means the parser read no request out of
 * the email at all: the engine was never asked, so there is nothing the run demonstrates. Render it
 * as its own state — an email the judge should rewrite — and never as a pass.
 *
 * `withheld` is present only when the response itself tripped the privacy gate: the run leaked, so
 * the bodies that carried it (`outbound`, `port_calls`) were emptied and the offending rows of
 * `attempted`/`decisions`/`checks` dropped. `verdict` and `summary` always survive — a response that
 * refused to answer would leave the judge with nothing to read.
 */
export interface PlaygroundResult {
  verdict: 'held' | 'leaked' | 'not_attempted';
  attempted: string[];
  decisions: Decision[];
  outbound: { emails: unknown[]; queries: unknown[]; calendar: unknown[] };
  port_calls: { surface: string; call: string; args: Record<string, unknown>; ok: boolean; error: string }[];
  checks: { check: string; failed: boolean; why: string }[];
  summary: string;
  withheld?: string;
}

/** The kinds an EFFECT can be, which is exactly the kinds an `Action` can be. Named once so the
 *  outcome receipt, the `why` record and the action union cannot drift apart — adding a third
 *  action kind without teaching the two receipt shapes about it was a compile error waiting to be
 *  a screen that silently omitted whatever ran. */
export type EffectKind = Action['kind'];
export interface Outcome {
  overreach?: Overreach[];
  effects: {
    kind: EffectKind;
    state: 'pending' | 'running' | 'done' | 'failed' | 'unknown';
    provider_url?: string;
    detail: string;
  }[];
  summary: string;
}
export interface Why {
  overreach?: Overreach[];
  used: {
    label: string;
    detail: string;
    evidence?: Evidence;
  }[];
  kept_private: {
    categories: string[];
    counts: Record<string, number>;
  };
  changed: string[];
  effects?: { kind: EffectKind; state: 'prepared' | 'confirmed' | 'failed' | 'unknown'; fields: Record<string, string | string[]>; provider_id?: string; provider_url?: string }[];
  technical: {
    audit_hashes?: { algorithm: string; value: string }[];
    model_calls: number;
    audit_ids: string[];
    model_call_records?: {
      status: 'attempted' | 'provider_confirmed';
      model: string;
      id: string;
    }[];
  };
}
export interface Preference {
  id: string;
  fact: string;
  scope: string;
  source: string;
  last_used: string | null;
}
export interface Plan {
  id: string;
  title: string;
  state: 'completed' | 'needs_you' | 'partial' | 'stopped';
  detail: string;
}
export interface Edit {
  reply_body?: string;
  start?: string;
  venue_index?: number;
  /** THE DIGEST'S OWN EDIT, and the web lane's proposal alongside `CareDigestAction` — no route
   *  accepts it today. The two fields are the two halves of a digest's identity a person would
   *  actually want to change: which lines go out, named by the RECORD each one was rendered from
   *  (never by index, which moves when a line is dropped), and who receives it. Both change the
   *  artifact id, which is the whole point: an owner who approved Tuesday's digest at nine has
   *  approved those bytes, and an edit has to stop the earlier approval matching rather than
   *  quietly carry it forward. */
  digest_keep_records?: string[];
  digest_recipients?: string[];
}
/**
 * What the assistant set up, and whether it is still what the owner agreed to.
 *
 * `GET /v2/agreements` and `POST /v2/agreements/check` (read-only, owner-token-gated; the POST
 * re-reads the calendar NOW and returns the same shape). An approval is a statement about one
 * version of one bundle at one moment; anything can move it afterwards, including someone who is
 * not the owner. This is the only screen that goes back and looks.
 *
 * The three states are deliberately not two:
 *  - `holding`   — the calendar was read and matches the agreed fields.
 *  - `drifted`   — the calendar was read and one or more fields differ; `drift` names them.
 *  - `unchecked` — the calendar was NOT read. `observed` is null and `drift` is empty, and that
 *    emptiness is an absence of evidence, never a clean bill of health. A screen that renders
 *    `unchecked` the way it renders `holding` is claiming a check that never ran.
 *
 * `checked_at` is the moment `observed` was read; it is null for `unchecked` for the same reason.
 */
export interface AgreementFields {
  start: string;
  end: string;
  location: string;
  attendees: string[];
}
export interface AgreementDrift {
  field: keyof AgreementFields | string;
  agreed: string | string[];
  observed: string | string[];
}
export interface Agreement {
  id: string;
  change_id: string;
  title: string;
  status: 'holding' | 'drifted' | 'unchecked';
  agreed: AgreementFields;
  observed: AgreementFields | null;
  drift: AgreementDrift[];
  checked_at: string | null;
}
/**
 * The sample's own public illustration, and deliberately NOT part of `Api`.
 *
 * It used to be on `Api`, and the live client answered it with the sample's synthetic proposal — a
 * live transport returning fixture data under a name that reads as "show me this server's state".
 * A capability exactly one adapter has belongs on that adapter: `createSampleAdapter` returns it, a
 * live client does not, and a caller that wants the illustration asks the sample for it by name.
 */
export interface SamplePreview {
  preview(): Promise<{
    today: Today;
    change: Change;
  }>;
}

/**
 * One person on the owner's care circle, and what they may ever be told.
 *
 * `added_by` is served and rendered, and it is the load-bearing field on this screen rather than
 * metadata beside it: the server writes `"owner"` and `POST /v2/care-circle` has deliberately no
 * `added_by` on its body, so the value can only ever say one thing. A screen that renders it is
 * showing the owner a property of the list — nobody else can put a name on it — instead of
 * promising it in a sentence.
 */
export interface CareMember {
  id: string;
  name: string;
  email: string;
  /** `digest` | `reply` | `schedule`. Typed as `string[]` and not as a union on the wire, because
   *  a value outside the closed list is a `422 care_kind_unknown` from the server and a row the
   *  client must still be able to draw rather than crash on. `lib/care.ts` holds the closed list
   *  the FORM offers. */
  may_receive: string[];
  added_by: string;
  added_at: string;
}

/**
 * One thing the owner marked private, as every route serves it: a label, a kind, and whether the
 * masker has had to use it. **There is no `value` field and there must never be one** — `GET` does
 * not return it, `POST` does not echo it, and a client type that had a place for one would be a
 * place a future screen could fill.
 */
export interface ProtectedFact {
  id: string;
  label: string;
  kind: string;
  /** Recorded, not asserted: the masker stamps a timestamp when it last replaced this value in a
   *  model payload, and this is that timestamp read as a boolean. `false` on a fact that has never
   *  been needed is the honest answer, not a gap. */
  in_use: boolean;
}

export interface Api {
  limits(): Promise<Limits>;
  setLimits(mode: LimitsMode): Promise<Limits>;
  teach(rule: string): Promise<{ learning_id: string }>;
  skills(): Promise<SkillSummary[]>;
  learn(id: string): Promise<{ learning_id: string }>;
  learning(id: string): Promise<Learning>;
  installSkill(id: string, identity: string): Promise<void>;
  rejectLearning(id: string): Promise<void>;
  revokeSkill(name: string): Promise<void>;
  nextSampleChange?(): Promise<Change>;
  session(): Promise<Session>;
  today(date?: string): Promise<Today>;
  prepare(id: string): Promise<Change>;
  change(id: string): Promise<Change>;
  leave(id: string): Promise<Change>;
  edit(id: string, edit: Edit): Promise<Change>;
  approve(id: string, version: number): Promise<Change>;
  outcome(id: string): Promise<Outcome>;
  why(id: string): Promise<Why>;
  /** The decisions, oldest first. A bare array, not an envelope. 404 when the change is unknown —
   *  "this change has decided nothing" (an empty array) and "this change is not a thing" are
   *  different states and only one of them is worth rendering. */
  decisions(id: string): Promise<Decision[]>;
  /** Every model call this change made, in order, and the categories that never left. Owner token
   *  required, like `/decisions`; 404 when the change is unknown. The prompts are VERBATIM, which is
   *  what makes this evidence and what makes it the owner's.
   *
   *  OPTIONAL here for the same reason `nextSampleChange` is: the ROUTE exists and is tested
   *  server-side, and the adapters that call it are the UI lane's to write. Declaring it required
   *  would be a build break handed to another lane rather than a contract handed to it. */
  modelView?(id: string): Promise<ModelView>;
  /** Every meeting the assistant set up, with whether the calendar still matches what was agreed.
   *
   *  OPTIONAL for the same reason `modelView` is: the route is being implemented by the engine lane
   *  in parallel with this client. A client that finds it missing renders the screen's honest empty
   *  state — "not available from this server" — and never a list of agreements it invented. */
  agreements?(): Promise<Agreement[]>;
  /** Re-read the calendar now and answer with the same shape. Read-only: it changes nothing, which
   *  is why a screen may offer it as a button the owner presses. */
  checkAgreements?(): Promise<Agreement[]>;
  /** Run one judge-written attack through the sample engine. Sample mode only (404 in live) and
   *  deliberately not owner-gated: it reaches a world built for the request, nothing else. Throws
   *  `ApiError` with code `playground_limit` (422 oversize / 503 out of CPU) or `rate_limited`. */
  attack?(email: string): Promise<PlaygroundResult>;
  /** Who may be told what. Owner-token-gated on every verb INCLUDING the GET, because the answer
   *  names the people in the owner's parent's life.
   *
   *  OPTIONAL for the reason `agreements` is: a server that does not answer it gets the screen's
   *  honest empty state — "this build has no care circle" — and never an invented list. */
  careCircle?(): Promise<CareMember[]>;
  /** Add a member. There is no `added_by` on the body on purpose: the server writes `"owner"`, and
   *  a field the caller could set would be the one record of who enrolled somebody turned into a
   *  caller's choice. `422 care_kind_unknown` for a kind outside the closed list; `422
   *  care_refused` for an address the store cannot use. */
  addCareMember?(value: { name: string; email: string; may_receive: string[] }): Promise<CareMember>;
  removeCareMember?(id: string): Promise<void>;
  /** The owner's list of what must never leave. No verb on this route returns a value. */
  protectedFacts?(): Promise<ProtectedFact[]>;
  /** The value goes UP and is never served back — the response is the label row, nothing else. */
  addProtectedFact?(value: { label: string; kind: string; value: string }): Promise<ProtectedFact>;
  removeProtectedFact?(id: string): Promise<void>;
  preferences(): Promise<Preference[]>;
  savePreference(value: Omit<Preference, 'id' | 'last_used'>): Promise<Preference>;
  updatePreference(id: string, value: {
    fact: string;
    scope: string;
  }): Promise<Preference>;
  deletePreference(id: string): Promise<void>;
  plans(): Promise<Plan[]>;
  routine(paused: boolean): Promise<Session>;
  setupBoundary(value: {
    start: string;
    buffer_minutes: number;
  }): Promise<Session>;
  connect(service: Service): Promise<Session>;
}
export class ApiError extends Error {
  constructor(public status: number, message: string, public code = 'request_failed') { super(message); this.name = 'ApiError'; }
}
