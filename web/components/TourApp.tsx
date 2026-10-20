'use client';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiMode, ApiError, ownerToken, setOwnerToken } from '@/lib/api';
import { safeHttpUrl } from '@/lib/urls';
import { LATER_CHANGE_ID, lookalikeChecks, resetSample } from '@/lib/sample';
import { buildFeed, isDigest, type FeedItem } from '@/lib/feed';
import type { Agreement, CareMember, Change, Today, Session, Outcome, Why, Preference, ProtectedFact, SampleVariant, Evidence, Limits, ModelView, SkillSummary } from '@/lib/v2-types';
import { CareCircle } from '@/components/CareCircle';
import { ProtectedFacts } from '@/components/ProtectedFacts';
import { artifactId } from '@/lib/care';
import { LearningPanel, OverreachList } from '@/components/Learning';
import type { Decision } from '@/lib/decisions';
import { StandingLimits } from '@/components/StandingLimits';
import { Teach } from '@/components/Teach';
import { Day, time } from '@/components/Day';
import { Shell, limitsModeLabel, autonomyLabel, type Place } from '@/components/Shell';
import { TodayFeed } from '@/components/TodayFeed';
import { ChangeDetail } from '@/components/ChangeDetail';
import { Skills } from '@/components/Skills';
import { Agreements } from '@/components/Agreements';
import { FirstRun } from '@/components/FirstRun';

/**
 * The app has six places and one drill-down. A place is a thing a person goes to on purpose;
 * `change` is not one of them — it is opened from a row in Today or from an agreement — which is why
 * it is tracked separately from `place` and why leaving it returns you to where you were.
 */
type Screen = Place | 'change' | 'why';
const preparingStages = ['reading_thread', 'checking_availability', 'checking_venues'];
const sampleMode = process.env.NEXT_PUBLIC_API_MODE !== 'live';
// The demo control belongs to the BACKEND being a sample, not to this build being one: a live
// client pointed at `privatemesh serve --v2 --sample` is the judges' path, and it needs the same
// button. Gating on the build alone left the second half of the claim — the second time, it does
// not ask again — unreachable from the path the judges are told to run.
const sampleControls = (mode: string | undefined) => sampleMode || mode === 'sample';
const variants: SampleVariant[] = ['happy', 'partial', 'unknown', 'stale', 'no_safe_fit', 'expired', 'missing', 'paused', 'empty', 'authorizing', 'expired_connection', 'long_wait', 'limit_handoff'];
const variantNames: Record<SampleVariant, string> = { happy: 'A plan that fits', partial: 'Reply fails after invite update', unknown: 'Reply delivery unknown', stale: 'Calendar changes before approval', no_safe_fit: 'No safe fit', expired: 'Expired approval', missing: 'Disconnected tools', paused: 'Routine paused', empty: 'No changes', authorizing: 'Gmail authorization in progress', expired_connection: 'Gmail connection expired', long_wait: 'Venue research taking longer', limit_handoff: 'A clause hands the change back' };

// The protected boundary the form shows before anything has been read. Named, because two places
// now need the same two values: the load, which falls back to them when the server reports none, and
// `clearPrivateState`, which puts them back when the owner forgets the credential — the loaded value
// is the owner's own availability, read from `GET /v2/session`, and it may not outlive the token it
// was read with.
const DEFAULT_BOUNDARY_START = '16:30';
const DEFAULT_BOUNDARY_BUFFER = 45;

function Source({ evidence }: { evidence: Evidence }) {
  // Scheme-checked before it reaches the `href`: only an absolute http(s) URL renders a link.
  const href = safeHttpUrl(evidence.url);
  return <div className="source">
    <strong>{evidence.sample ? 'Sample source' : evidence.official ? 'Official source' : 'Public source'} · {evidence.title}</strong>
    <p>{evidence.excerpt}</p>
    <small>Checked {new Date(evidence.retrieved_at).toLocaleString('en-GB', { timeZone: 'Europe/London' })} · Europe/London</small>
    {href && <a href={href} target="_blank" rel="noreferrer">Read source</a>}
  </div>;
}

export default function TourApp() {
  const [screen, setScreen] = useState<Screen>('today');
  const [settingsTab, setSettingsTab] = useState<'connections' | 'preferences' | 'demo'>('connections');
  // One place, two lists. They are tabs rather than two nav slots because they are two halves of
  // one question — who may be told what, and what must never be told to anyone — and a six-item
  // tab bar that split them would make the phone nav the widest thing on the screen.
  const [careTab, setCareTab] = useState<'circle' | 'protected'>('circle');
  const [session, setSession] = useState<Session | null>(null);
  // Whether THIS WORKSPACE IS SYNTHETIC follows the backend's `mode`, not which client was built.
  // `docs/API_V2.md` defines `mode` as "sample (synthetic data, simulators, labelled) or live", so
  // the backend is the only party that knows, and the honesty labels have to come from it.
  // `sampleMode` still decides the LOCAL fixture controls (the scenario switcher, the `variant`
  // query), which are the client's own and meaningless against a real backend.
  const sampleWorkspace = sampleControls(session?.mode);
  const [today, setToday] = useState<Today | null>(null);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [allChanges, setAllChanges] = useState<Change[]>([]);
  const [historyEmpty, setHistoryEmpty] = useState(false);
  const [waiting, setWaiting] = useState(0);
  const [change, setChange] = useState<Change | null>(null);
  const [decisionState, setDecisionState] = useState<{ id: string; records: Decision[]; unavailable: boolean } | null>(null);
  const [modelView, setModelView] = useState<{ id: string; view: ModelView | null; state: 'loading' | 'ready' | 'forbidden' | 'unavailable' } | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [why, setWhy] = useState<Why | null>(null);
  const [preferences, setPreferences] = useState<Preference[]>([]);
  const [limitsMode, setLimitsMode] = useState<Limits['mode'] | null>(null);
  const [limitsChosen, setLimitsChosen] = useState(false);
  // Whether the standing answer was actually READ. `limitsMode === null` used to mean two different
  // things — "not asked yet" and "asked and refused" — and the Today rail resolved both to
  // `ask_always`, so a live build that could not read the limits showed the owner the most
  // conservative-sounding autonomy there is. That is a DEFAULT presented as an ANSWER, on the one
  // control that decides how much the assistant may do without asking. Three states, three words.
  const [limitsRead, setLimitsRead] = useState<'idle' | 'loading' | 'ready' | 'failed'>('idle');
  // The owner token is entered here, held in memory for this tab. See `lib/api.ts` for why it is not
  // a build-time value; the field below is the only way the client ever gets one.
  const [tokenDraft, setTokenDraft] = useState('');
  const [hasToken, setHasToken] = useState(() => ownerToken() !== null);
  const [limitCatalogue, setLimitCatalogue] = useState<Limits['clauses']>([]);
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [agreements, setAgreements] = useState<Agreement[]>([]);
  const [agreementsUnavailable, setAgreementsUnavailable] = useState(false);
  const [circle, setCircle] = useState<CareMember[]>([]);
  const [circleUnavailable, setCircleUnavailable] = useState(false);
  const [facts, setFacts] = useState<ProtectedFact[]>([]);
  const [factsUnavailable, setFactsUnavailable] = useState(false);
  const [careError, setCareError] = useState('');
  // The artifact the owner approved on a digest, kept for the length of this tab. It is what makes
  // "your earlier approval no longer matches" a comparison rather than an assertion: the digest
  // screen recomputes the id of what is on screen and holds it against this.
  const [approvedArtifacts, setApprovedArtifacts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [offline, setOffline] = useState(false);
  const [stale, setStale] = useState(false);
  const [editing, setEditing] = useState(false);
  const [reply, setReply] = useState('');
  const [editTime, setEditTime] = useState('15:00');
  const [venueIndex, setVenueIndex] = useState(0);
  const [variant, setVariant] = useState<SampleVariant>('happy');
  const [waited, setWaited] = useState(false);
  const [lastLoaded, setLastLoaded] = useState('');
  const [clock, setClock] = useState(Date.now());
  const [boundaryTime, setBoundaryTime] = useState(DEFAULT_BOUNDARY_START);
  const [buffer, setBuffer] = useState(DEFAULT_BOUNDARY_BUFFER);
  const [fact, setFact] = useState('');
  const [editingPreference, setEditingPreference] = useState<string | null>(null);
  const [scope, setScope] = useState('Meeting planning');
  const heading = useRef<HTMLHeadingElement>(null);
  const generation = useRef(0);
  const running = useRef(false);
  const lastFocused = useRef<HTMLElement | null>(null);

  const proposal = change?.proposal;
  const mail = proposal?.actions.find(a => a.kind === 'email_reply');
  const calendar = proposal?.actions.find(a => a.kind === 'calendar_update');
  const isPreparing = preparingStages.includes(change?.stage ?? '');
  const isExecuting = change?.stage === 'approved' || change?.stage === 'executing';
  const isOutcome = change?.stage === 'done' || change?.stage === 'partial';
  const expired = !!proposal && (!Number.isFinite(Date.parse(proposal.expires_at)) || Date.parse(proposal.expires_at) <= clock);
  // What a COMPLETE bundle looks like, and there are two shapes of it now.
  //
  // A scheduling change is exactly two actions — the calendar edit and the reply — and anything
  // else is a plan the owner must not be offered a button for. A digest is exactly one: the
  // outbound artifact, with an audience and at least the composer's line and one more. Reading
  // the first rule over the second is not a cosmetic mismatch: `approve` returns early when this
  // is false, so a digest would have had an enabled Approve button that silently did nothing.
  const digestAction = proposal?.actions.find(a => a.kind === 'care_digest');
  const validBundle = !!proposal && (digestAction?.kind === 'care_digest'
    ? proposal.actions.length === 1 && digestAction.to.length > 0 && digestAction.fragments.length > 1 && Number.isInteger(change?.version)
    : proposal.actions.length === 2 && !!mail && !!calendar && Array.isArray(calendar.attendees) && Number.isInteger(change?.version));
  const summary = change ? today?.changes.find(c => c.id === change.id) : undefined;
  // Whether the open change ENDED in a refusal — the same test `ChangeDetail` makes, so the
  // heading and the body cannot come to describe the screen differently. See the note there for
  // why only a `send`/`execute` refusal counts.
  const changeRefused = change?.stage === 'no_safe_fit'
    && (decisionState?.id === change.id ? decisionState.records : []).some(d => d.verdict === 'refused' && (d.stage === 'send' || d.stage === 'execute'));
  // First run is "this owner has no history at all", not "nothing needs you today". A setup screen
  // that reappears on a quiet day is the fastest way to make a product feel like a demo.
  // `setupDismissed` is the owner's own "Start using PrivateMesh" — a real ending, not just a
  // checklist that goes quiet, so completing the three steps always leads somewhere.
  const [setupDismissed, setSetupDismissed] = useState(false);
  const firstRun = !!today && historyEmpty && !setupDismissed;

  const run = async (work: () => Promise<void>) => {
    if (running.current) return;
    if (offline) { setError('You are offline. Reconnect before changing this plan.'); return; }
    generation.current++; running.current = true; setBusy(true); setError('');
    try { await work(); }
    catch (e) { setError(e instanceof Error ? e.message : 'This request could not finish. Please try again.'); }
    finally { running.current = false; setBusy(false); }
  };

  /** The feed: today's changes UNION everything in `/plans`, then one `GET /changes/{id}` each.
   *
   *  Two reasons for the union and the extra reads. A day's `changes` are the ones still attached to
   *  today, while `/plans` is the history — a feed built from either alone is a to-do list, and the
   *  product is supposed to read as a record of work done. And the summary row carries neither the
   *  proposal nor `blocked_attempts`, so a feed built from it could only say "a change exists".
   *  Failures are tolerated per row: one unreadable change must not empty the day. */
  const loadFeed = useCallback(async (day: Today) => {
    const plans = await api.plans().catch(() => []);
    // Read once, at the start, and compared before this function writes anything: the caller's own
    // guard runs before IT writes, and these four `set*` calls are the ones that would put a private
    // day back on a screen the owner has since cleared (see `forgetOwnerToken`).
    const feedGeneration = generation.current;
    // Today's ids go LAST. The order is arbitrary to a real server, and putting them last is what
    // keeps the synthetic adapter coherent: its `change` is a cursor that each read moves, so the
    // last read decides which change `GET /today` describes next. Ending on today's own change is
    // the only order that leaves it where it started.
    const ids = [...new Set([...plans.map(plan => plan.id), ...day.changes.map(row => row.id)])];
    if (feedGeneration !== generation.current) return [];
    setHistoryEmpty(ids.length === 0);
    const settled = await Promise.allSettled(ids.map(id => api.change(id)));
    const changes = settled.flatMap(result => result.status === 'fulfilled' ? [result.value] : []);
    const needsLog = changes.filter(c => c.blocked_attempts > 0);
    const logs = await Promise.allSettled(needsLog.map(c => api.decisions(c.id)));
    const decisions: Record<string, Decision[]> = {};
    needsLog.forEach((c, index) => {
      const result = logs[index];
      if (result?.status === 'fulfilled') decisions[c.id] = result.value;
    });
    // The outcome is read for every finished change, not only the selected one. A Today row that
    // says "partial" has to say WHICH part, and the only place that fact exists is `GET /outcome`:
    // the stage alone cannot distinguish a reply that failed from a reply whose delivery was never
    // confirmed, and the row that guessed said the first about the second. Same tolerance as above —
    // one unreadable outcome leaves its row claiming nothing, rather than emptying the day.
    const finished = changes.filter(c => c.stage === 'done' || c.stage === 'partial');
    const outcomeRows = await Promise.allSettled(finished.map(c => api.outcome(c.id)));
    const outcomes: Record<string, Outcome> = {};
    finished.forEach((c, index) => {
      const result = outcomeRows[index];
      if (result?.status === 'fulfilled') outcomes[c.id] = result.value;
    });
    if (feedGeneration !== generation.current) return [];
    const items = buildFeed(day, changes, decisions, outcomes);
    setFeed(items);
    setAllChanges(changes);
    // The counter must say exactly what "Needs you" renders — never a stage-based count computed
    // separately from it, which drifted from the rendered queue the moment a stage (like `partial`)
    // counted here but rendered under Recent activity instead.
    setWaiting(items.filter(item => item.status === 'needs_you').length);
    return changes;
  }, []);

  /** Everything on this screen that came from the owner's own data, put back to "nothing loaded".
   *
   *  Forgetting the credential has to clear these IN THE SAME TURN as the token, and that is the
   *  sixth review's Medium. The button used to clear the token and call `load` — but in live mode a
   *  tokenless `api.session()` is REFUSED, so `load` rejected at its first line and every `set*`
   *  under it never ran: the prior day, the limits and the owner controls stayed on screen, drawn
   *  from memory, under a form that now said no token was held. Two claims on one screen and the
   *  private one was the stale one.
   */
  const clearPrivateState = useCallback(() => {
    setSession(null);
    setToday(null);
    setFeed([]);
    setAllChanges([]);
    setHistoryEmpty(false);
    setWaiting(0);
    setChange(null);
    setDecisionState(null);
    setModelView(null);
    setOutcome(null);
    setWhy(null);
    setPreferences([]);
    setLimitsMode(null);
    setLimitsChosen(false);
    setLimitsRead('idle');
    setLimitCatalogue([]);
    setSkills([]);
    setAgreements([]);
    setAgreementsUnavailable(false);
    // The circle names the owner's family and the fact list names what they keep private. Both are
    // owner-gated reads, so both are on this list for exactly the reason the day is: leaving them
    // drawn from memory under a status line saying no token is held is two claims on one screen,
    // and the private one would be the stale one.
    setCircle([]);
    setCircleUnavailable(false);
    setFacts([]);
    setFactsUnavailable(false);
    setCareError('');
    setApprovedArtifacts({});
    setStale(false);
    setEditing(false);
    // Two form fields are on this list for the same reason the day is: their VALUE came from the
    // server, not from the owner's typing. `boundaryTime` and `buffer` are the owner's protected
    // availability, read from `GET /v2/session`, and leaving them in the form under a status line
    // that says no token is held is the same two-claims-one-screen defect as the stale day — the
    // private one being the stale one. `reply` is here for the neighbouring reason: it is text
    // drafted against a plan that is no longer on screen.
    setBoundaryTime(DEFAULT_BOUNDARY_START);
    setBuffer(DEFAULT_BOUNDARY_BUFFER);
    setReply('');
    setLastLoaded('');
    setError('');
  }, []);

  /** Forget the owner credential. See `clearPrivateState` for why the clear is not left to `load`.
   *
   *  `generation` is bumped FIRST. A `load` already in flight when the owner clicks this checks the
   *  generation before it writes anything, so bumping it is what makes the clear below final rather
   *  than a race the in-flight response can undo.
   *
   *  The DRAFT goes with it, which is the seventh review's Medium. `tokenDraft` holds whatever is in
   *  the password field — and the owner who pastes a replacement token and then clicks Forget, or
   *  clicks Forget without pasting at all, was left with a live credential sitting in a field under
   *  a status line reading "No token is held". That is the same two-claims-one-screen defect
   *  `clearPrivateState` exists for, so it is cleared in the same turn; it is cleared HERE rather
   *  than there because `clearPrivateState` is about what the SERVER sent and this is about what the
   *  owner typed, and only one of the two is a credential. */
  const forgetOwnerToken = useCallback(() => {
    generation.current++;
    setOwnerToken(null);
    setHasToken(false);
    setTokenDraft('');
    clearPrivateState();
  }, [clearPrivateState]);

  const load = useCallback(async () => {
    const requestGeneration = generation.current;
    const [s, t] = await Promise.all([api.session(), api.today()]);
    const changes = await loadFeed(t);
    // The default open change is the one actually attached to TODAY — the last entry `GET /today`
    // reports — never just "whichever id happened to load first". A believable week's worth of
    // settled history in `plans()` would otherwise win that race and land the owner on a stale
    // change the moment they open the app.
    const liveId = t.changes.at(-1)?.id;
    const selected = changes.find(c => c.id === liveId) ?? changes[0] ?? null;
    const result = selected && ['done', 'partial', 'approved', 'executing'].includes(selected.stage) ? await api.outcome(selected.id) : null;
    if (requestGeneration !== generation.current) return;
    setSession(s);
    setToday(t);
    setBoundaryTime(s.boundary?.start ?? DEFAULT_BOUNDARY_START);
    setBuffer(s.boundary?.buffer_minutes ?? DEFAULT_BOUNDARY_BUFFER);
    setLastLoaded(new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }));
    setChange(selected);
    setOutcome(result);
  }, [loadFeed]);

  // Reading the standing answer is three outcomes, not two. `authenticated_owner` false is the
  // third: `/limits` is owner-gated, so a live build without a token is REFUSED rather than empty,
  // and the rail must say the answer is unknown rather than pick one.
  // `active` is not decoration and not symmetry with the two effects below it. This read is
  // owner-gated, and the owner can withdraw the credential while it is in flight — the same turn
  // `clearPrivateState` runs in. Without the guard the answer lands afterwards and writes the
  // owner's standing autonomy back into state, where the app bar renders it on every screen: the
  // chip says "Ask me every time" directly above a status line saying no token is held. The
  // cleanup is what makes the clear final rather than merely first.
  useEffect(() => {
    if (!session) return;
    if (!session.authenticated_owner) { setLimitsRead('failed'); return; }
    let active = true;
    setLimitsRead('loading');
    void api.limits()
      .then(value => {
        if (!active) return;
        setLimitCatalogue(value.clauses); setLimitsMode(value.mode); setLimitsChosen(value.updated_at !== null); setLimitsRead('ready');
      })
      .catch(() => { if (active) { setLimitCatalogue([]); setLimitsMode(null); setLimitsRead('failed'); } });
    return () => { active = false; };
  }, [session?.authenticated_owner, session]);

  useEffect(() => {
    if (!change) return;
    let active = true;
    const id = change.id;
    void api.decisions(id).then(records => { if (active) setDecisionState({ id, records, unavailable: false }); })
      .catch(() => { if (active) setDecisionState({ id, records: [], unavailable: true }); });
    return () => { active = false; };
  }, [change?.id, change?.stage, change?.version]);

  // The model receipt is read only for the change the owner actually opened. It is owner-gated
  // server-side, so a 403 is a state worth distinguishing from a missing route: one says "not
  // yours to read", the other says "this server does not answer that question". Neither says
  // "nothing was sent", which is the one reading this panel must never produce.
  useEffect(() => {
    if (screen !== 'change' || !change) return;
    const id = change.id;
    if (!api.modelView) { setModelView({ id, view: null, state: 'unavailable' }); return; }
    let active = true;
    setModelView(current => current?.id === id ? current : { id, view: null, state: 'loading' });
    void api.modelView(id).then(view => { if (active) setModelView({ id, view, state: 'ready' }); })
      .catch(e => { if (active) setModelView({ id, view: null, state: e instanceof ApiError && (e.status === 401 || e.status === 403) ? 'forbidden' : 'unavailable' }); });
    return () => { active = false; };
  }, [screen, change?.id, change?.stage, change?.version]);

  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const v = query.get('variant');
    if (sampleMode && v && variants.includes(v as SampleVariant)) {
      resetSample(v as SampleVariant);
      setVariant(v as SampleVariant);
    }
    void load().catch(e => setError(String(e.message)));
  }, [load]);

  useEffect(() => { setOffline(!navigator.onLine); const off = () => setOffline(true); const on = () => setOffline(false); window.addEventListener('offline', off); window.addEventListener('online', on); return () => { window.removeEventListener('offline', off); window.removeEventListener('online', on); }; }, []);
  useEffect(() => { if (lastFocused.current && !lastFocused.current.isConnected) heading.current?.focus({ preventScroll: true }); }, [change?.stage, editing, busy]);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); window.scrollTo(0, 0); }, [screen]);
  useEffect(() => { const timer = setInterval(() => setClock(Date.now()), 1000); return () => clearInterval(timer); }, []);
  useEffect(() => { setWaited(false); if (!isPreparing) return; const timer = setTimeout(() => setWaited(true), 15000); return () => clearTimeout(timer); }, [isPreparing]);

  useEffect(() => {
    if (!change || !(isPreparing || isExecuting)) return;
    let active = true;
    let pending = false;
    const tick = async () => {
      if (pending || busy || offline) return;
      pending = true;
      const requestGeneration = generation.current;
      try {
        const next = await api.change(change.id);
        const result = ['done', 'partial', 'approved', 'executing'].includes(next.stage) ? await api.outcome(next.id) : null;
        if (!active || requestGeneration !== generation.current) return;
        setChange(current => current?.id === next.id && current.version > next.version ? current : next);
        if (result) setOutcome(result);
      }
      catch (e) {
        if (active && requestGeneration === generation.current)
          setError(`Connection interrupted. Showing data loaded at ${lastLoaded}. ${e instanceof Error ? e.message : ''}`);
      }
      finally { pending = false; }
    };
    const timer = setInterval(() => { void tick(); }, 600);
    return () => { active = false; clearInterval(timer); };
  }, [change?.id, isPreparing, isExecuting, lastLoaded, busy, offline]);

  useEffect(() => {
    if (screen !== 'settings' || !session?.connections.some(c => c.state === 'authorizing')) return;
    let active = true;
    let pending = false;
    const timer = setInterval(async () => {
      if (pending || offline) return;
      pending = true;
      try { const next = await api.session(); if (active) setSession(next); }
      catch { if (active) setError('Connection status could not refresh. Check your connection and try again.'); }
      finally { pending = false; }
    }, 1500);
    return () => { active = false; clearInterval(timer); };
  }, [screen, session?.connections, offline]);

  const loadSkills = useCallback(async () => {
    try { setSkills(await api.skills()); }
    catch { setError('The skills library could not load. Refresh to try again.'); }
  }, []);
  const loadAgreements = useCallback(async () => {
    if (!api.agreements) { setAgreementsUnavailable(true); return; }
    try { setAgreements(await api.agreements()); setAgreementsUnavailable(false); }
    catch { setAgreementsUnavailable(true); }
  }, []);
  /** The two care lists. Each fails to its own "not available from this server" rather than to an
   *  empty list, for the reason the gate itself fails closed: a missing circle must not read as an
   *  absent restriction. They are loaded TOGETHER because the digest screen needs the circle to
   *  check its own audience, and that screen is reachable from Today without ever visiting Care.
   *
   *  Review finding, Medium: this used to write straight into `circle`/`facts` with no `generation`
   *  check — the same guard every other read in this file uses (see `load`, `forgetOwnerToken`).
   *  `careCircle()`/`protectedFacts()` are the owner's family list and protected facts; a request
   *  still in flight when the owner clicks "Forget owner token" resolved afterwards and put that
   *  private data straight back on a screen that had just said no token was held. Each write below
   *  is now checked against the generation it was STARTED under, so a stale answer is dropped. */
  const loadCare = useCallback(async () => {
    const requestGeneration = generation.current;
    if (api.careCircle) {
      try {
        const members = await api.careCircle();
        if (requestGeneration !== generation.current) return;
        setCircle(members); setCircleUnavailable(false);
      }
      catch {
        if (requestGeneration !== generation.current) return;
        setCircle([]); setCircleUnavailable(true);
      }
    } else setCircleUnavailable(true);
    if (requestGeneration !== generation.current) return;
    if (api.protectedFacts) {
      try {
        const protectedFacts = await api.protectedFacts();
        if (requestGeneration !== generation.current) return;
        setFacts(protectedFacts); setFactsUnavailable(false);
      }
      catch {
        if (requestGeneration !== generation.current) return;
        setFacts([]); setFactsUnavailable(true);
      }
    } else setFactsUnavailable(true);
  }, []);

  const refreshDay = async () => { const day = await api.today(); setToday(day); await loadFeed(day); };

  const navigate = (next: Place) => {
    if (running.current) return;
    setError('');
    setScreen(next);
    // Today is a live record, so it is re-read on arrival. A change that finished while it was open
    // updates `change` through the poller and would otherwise leave the row behind it saying
    // "Running" — the one thing a feed of what already happened must never do.
    if (next === 'today') void refreshDay().catch(e => setError(e instanceof Error ? e.message : 'Today could not refresh.'));
    if (next === 'settings') void run(async () => setPreferences(await api.preferences()));
    if (next === 'skills') void loadSkills();
    if (next === 'agreements') void loadAgreements();
    if (next === 'care') { setCareError(''); void loadCare(); }
  };

  const openChange = (id: string) => {
    if (running.current) return;
    setError('');
    setEditing(false);
    void run(async () => {
      const selected = await api.change(id);
      const result = ['done', 'partial', 'approved', 'executing'].includes(selected.stage) ? await api.outcome(id) : null;
      setChange(selected);
      setOutcome(result);
      setScreen('change');
    });
  };


  const prepare = () => run(async () => {
    if (!change) return;
    setStale(false); setOutcome(null);
    const prepared = await api.prepare(change.id);
    setChange(prepared);
    if (['done', 'partial', 'approved', 'executing'].includes(prepared.stage)) setOutcome(await api.outcome(prepared.id));
    await refreshDay();
  });

  // No separate "review" step. The exact calendar diff and the exact reply are already on screen —
  // that IS the review — so pressing Approve applies to whatever version is currently shown.
  const approve = () => run(async () => {
    if (!change || stale || expired || !session?.authenticated_owner || !validBundle) return;
    try {
      const digest = change.proposal?.actions.find(a => a.kind === 'care_digest');
      if (digest?.kind === 'care_digest')
        // Recorded from the BYTES, not from the field the server sent: what the owner approved is
        // what was rendered to them, and holding the server's own label here would make the later
        // comparison a comparison of the server with itself.
        setApprovedArtifacts(current => ({ ...current, [change.id]: artifactId({ day: digest.day, recipients: digest.to, fragments: digest.fragments }) }));
      const approved = await api.approve(change.id, change.version);
      // The live backend finishes the effects inside the approval request, so the change comes back
      // already `done` and the poller never starts — it would render an outcome panel from state the
      // approval never loaded, which is a heading with nothing under it.
      const result = ['done', 'partial', 'approved', 'executing'].includes(approved.stage) ? await api.outcome(approved.id) : null;
      setOutcome(result);
      setChange(approved);
      await refreshDay();
      await loadAgreements();
    }
    catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.code !== 'limits_off') {
        setStale(true);
        setChange(await api.change(change.id));
      }
      throw e;
    }
  });

  const leave = () => run(async () => { if (!change) return; setChange(await api.leave(change.id)); await refreshDay(); });
  const editDigest = (edit: { digest_keep_records?: string[]; digest_recipients?: string[] }) => run(async () => {
    if (!change) return;
    setChange(await api.edit(change.id, edit));
    setStale(false);
    await refreshDay();
  });
  const openWhy = () => run(async () => { if (change) { setWhy(await api.why(change.id)); setScreen('why'); } });
  const reset = (v: SampleVariant) => run(async () => {
    resetSample(v); setLimitsMode('ask_always'); setLimitsChosen(false); setVariant(v); setOutcome(null); setStale(false); setEditing(false);
    await load(); await loadAgreements(); setScreen('today');
  });

  useEffect(() => { void loadAgreements(); }, [loadAgreements]);
  useEffect(() => { void loadCare(); }, [loadCare]);

  const title = useMemo(() => {
    switch (screen) {
      case 'today': return firstRun ? 'Set it up once.' : 'Today';
      case 'care': return careTab === 'circle' ? 'Who may be told about Rosa?' : 'What must never leave.';
      case 'agreements': return 'Agreement monitoring';
      case 'skills': return 'Teach it once. Keep control.';
      case 'limits': return 'How much can it do on its own?';
      case 'settings': return 'Your tools. Your boundaries.';
      case 'why': return 'Why this plan?';
      case 'change': {
        // Three kinds of change, three headings. The one heading this page used to have —
        // "<sender>'s meeting needs a new plan" — was a scheduling sentence, and it read as
        // "Benefits Update's meeting needs a new plan" over a refused attack and "This
        // assistant's meeting needs a new plan" over a digest nobody sent.
        if (change && isDigest(change)) return isOutcome ? outcome?.summary ?? 'Your digest is settling.' : 'Today’s family digest';
        if (changeRefused) return `${summary?.from_name ?? 'An email'} asked for something you do not allow.`;
        return isOutcome ? outcome?.summary ?? 'Your plan is settling.'
          : `${summary?.from_name ?? 'This'}’s request needs a new plan`;
      }
    }
  }, [screen, careTab, firstRun, isOutcome, outcome?.summary, summary?.from_name, change, changeRefused]);

  const eyebrow = screen === 'change' ? 'Change' : today ? `${today.date} · ${today.timezone}` : 'PrivateMesh';

  const editForm = proposal && today && <form noValidate className="card edit-form" onSubmit={e => {
    e.preventDefault();
    const submittedTime = String(new window.FormData(e.currentTarget).get('meeting_time') ?? '');
    void run(async () => {
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(submittedTime)) throw new Error('Choose a valid meeting time before revalidating your protected buffer.');
      if (!reply.trim()) throw new Error('The reply cannot be empty.');
      const displayed = time(proposal.start, today.timezone);
      const [hours, minutes] = submittedTime.split(':').map(Number);
      const [oldHours, oldMinutes] = displayed.split(':').map(Number);
      const editedStart = new Date(Date.parse(proposal.start) + ((hours! * 60 + minutes!) - (oldHours! * 60 + oldMinutes!)) * 60000).toISOString();
      if (Date.parse(editedStart) + 45 * 60000 > Date.parse(proposal.protected.start) - proposal.protected.buffer_minutes * 60000)
        throw new Error('That time crosses your protected boundary or buffer. Choose a time that fits.');
      setChange(await api.edit(change!.id, { reply_body: reply, start: editedStart, venue_index: venueIndex }));
      setEditing(false);
      setStale(false);
    });
  }}>
    <h2>Edit this proposal</h2>
    <p>Saving creates a new version. Any previous approval is invalidated.</p>
    <label>Meeting time<input name="meeting_time" type="time" value={editTime} onChange={e => {
      const next = e.target.value;
      const end = (value: string) => { const [h, m] = value.split(':').map(Number); const total = h! * 60 + m! + 45; return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`; };
      setReply(body => body.replaceAll(`${editTime}–${end(editTime)}`, `${next}–${end(next)}`));
      setEditTime(next);
    }} required /></label>
    <label>Venue<select value={venueIndex} onChange={e => {
      const index = Number(e.target.value);
      const venue = proposal.venues[index];
      if (venue)
        setReply(body => body.replaceAll(proposal.venues[venueIndex]?.name ?? proposal.venue.name, venue.name).replaceAll((proposal.venues[venueIndex]?.address ?? proposal.venue.address).split(',')[0]!, venue.address.split(',')[0]!));
      setVenueIndex(index);
    }}>{proposal.venues.map((v, i) => <option key={v.name} value={i}>{v.name}</option>)}</select></label>
    <label>Exact reply<textarea rows={7} value={reply} onChange={e => setReply(e.target.value)} required /></label>
    <div className="button-row">
      <button type="submit" className="primary" disabled={busy}>Save and revalidate</button>
      <button type="button" onClick={() => setEditing(false)}>Cancel edit</button>
    </div>
  </form>;

  const banner = <>
    {offline && <div className="alert attention" role="status"><strong>You’re offline.</strong><p>Showing your last loaded day from {lastLoaded || 'this session'}. Reconnect to refresh or approve. Your existing data stays here.</p></div>}
    {error && <div className="alert error" role="alert"><strong>{stale ? 'This approval is out of date.' : 'This needs your attention.'}</strong><p>{error}</p>{!stale && <button onClick={() => void run(load)}>Refresh workspace</button>}</div>}
  </>;

  return <Shell
    place={screen === 'change' || screen === 'why' ? 'today' : screen}
    onNavigate={navigate}
    disabled={!today || busy}
    mode={limitsMode}
    owner={session?.owner_name ?? null}
    sample={sampleWorkspace}
    banner={banner}
    firstRun={firstRun && screen === 'today'}
  >
    <div className="page-heading" onFocusCapture={event => { lastFocused.current = event.target as HTMLElement; }}>
      <p className="eyebrow">{eyebrow}</p>
      <h1 ref={heading} tabIndex={-1}>{title}</h1>
      {screen === 'today' && !firstRun && <p className="lead">PrivateMesh is a private assistant for the things you can’t afford to get wrong — nothing it reads, is told by a model, or is taught can ever widen what it may do or reveal.</p>}
    </div>
    {!today && !error && <p role="status">Opening your workspace…</p>}

    <div onFocusCapture={event => { lastFocused.current = event.target as HTMLElement; }}>
      {today && screen === 'today' && (firstRun
        ? <FirstRun session={session} mode={limitsMode} chosen={limitsChosen} taught={skills.some(s => s.state === 'active')} sample={sampleWorkspace} onConnect={() => navigate('settings')} onLimits={() => navigate('limits')} onTeach={() => navigate('skills')} onDone={() => setSetupDismissed(true)} />
        : <TodayFeed items={feed} session={session} waiting={waiting} busy={busy} onOpen={openChange} today={today.date} timezone={today.timezone} aside={<>
          {/* The schedule is today's actual context; the standing answer is a setting. One is what
              is happening, the other is a control — the fix for a rail that gave both the same
              card weight. The schedule leads; the mode is a compact status line beneath it. */}
          <Day today={today} change={change} outcome={outcome} compact />
          {/* A labeled permission control, not explanatory copy: "Autonomy" names the setting, its
              value is the standing answer, and "Manage limits" is a control's own action — not
              "Change", which read as vague next to a setting this consequential. */}
          <section className="card standing-card standing-compact">
            <span className="standing-label secondary">Autonomy</span>
            {/* Never a default in an answer's clothes. "Ask before acting" is the most permissive
                reading of an unread setting — it sounds careful while describing a value nobody
                loaded — so an unread setting says it is unread. */}
            <span className={`standing-answer${limitsMode ? '' : ' standing-unknown'}`}>{limitsMode
              ? autonomyLabel[limitsMode]
              : limitsRead === 'failed' ? 'Unknown — your limits could not be read' : 'Reading your limits…'}</span>
            <button className="text-button" onClick={() => navigate('limits')}>Manage limits</button>
          </section>
        </>} />)}

      {today && screen === 'change' && (change
        ? <ChangeDetail
          today={today}
          change={change}
          summary={summary}
          decisions={decisionState?.id === change.id ? decisionState : null}
          clauses={limitCatalogue}
          outcome={outcome}
          modelView={modelView?.id === change.id ? modelView.view : null}
          modelViewState={modelView?.id === change.id ? modelView.state : 'loading'}
          stale={stale}
          expired={expired}
          validBundle={validBundle}
          owner={session?.authenticated_owner === true}
          busy={busy}
          offline={offline}
          sample={sampleWorkspace}
          editing={editing}
          waited={waited}
          preparing={isPreparing}
          executing={isExecuting}
          onBack={() => setScreen('today')}
          onPrepare={prepare}
          onApprove={approve}
          onLeave={leave}
          onStartEdit={() => { setReply(mail?.body ?? ''); setEditTime(time(proposal!.start, today.timezone)); setVenueIndex(proposal!.venues.findIndex(v => v.name === proposal!.venue.name)); setEditing(true); }}
          onWhy={openWhy}
          editForm={editForm}
          learning={<LearningPanel key={`${variant}-${change.id}`} change={change} disabled={busy || offline} owner={session?.authenticated_owner === true} onPrepared={setChange} />}
          circle={circle}
          checks={sampleWorkspace ? lookalikeChecks() : []}
          approvedArtifact={approvedArtifacts[change.id] ?? null}
          onEditDigest={editDigest}
          afterActions={sampleControls(session?.mode) && change.stage === 'done' && change.id !== LATER_CHANGE_ID && api.nextSampleChange
            ? <div className="next-sample"><p>Next time, start with what you taught.</p><button disabled={busy} onClick={() => void run(async () => {
              if (!api.nextSampleChange) return;
              setChange(await api.nextSampleChange());
              setOutcome(null);
              await refreshDay();
            })}>Open a later sample change</button></div>
            : null}
        />
        : <p role="status">This change is no longer open. <button className="text-button" onClick={() => setScreen('today')}>Back to Today</button></p>)}

      {screen === 'care' && <section className="settings-page">
        <nav className="settings-tabs" aria-label="Care sections">
          <button aria-current={careTab === 'circle' ? 'page' : undefined} onClick={() => setCareTab('circle')}>Care circle</button>
          <button aria-current={careTab === 'protected' ? 'page' : undefined} onClick={() => setCareTab('protected')}>Protected facts</button>
        </nav>
        {careTab === 'circle'
          ? <CareCircle
            members={circle}
            unavailable={circleUnavailable}
            busy={busy}
            owner={session?.authenticated_owner === true}
            error={careError}
            onAdd={value => void run(async () => {
              if (!api.addCareMember) throw new Error('This server does not accept new care-circle members.');
              setCareError('');
              try { await api.addCareMember(value); }
              catch (e) { setCareError(e instanceof Error ? e.message : 'That member could not be added.'); }
              await loadCare();
            })}
            onRemove={member => void run(async () => {
              if (!api.removeCareMember) throw new Error('This server does not accept care-circle removals.');
              setCareError('');
              try { await api.removeCareMember(member.id); }
              catch (e) { setCareError(e instanceof Error ? e.message : 'That member could not be removed.'); }
              await loadCare();
            })}
          />
          : <ProtectedFacts
            facts={facts}
            unavailable={factsUnavailable}
            busy={busy}
            owner={session?.authenticated_owner === true}
            error={careError}
            onAdd={value => void run(async () => {
              if (!api.addProtectedFact) throw new Error('This server does not accept new protected facts.');
              setCareError('');
              try { await api.addProtectedFact(value); }
              catch (e) { setCareError(e instanceof Error ? e.message : 'That fact could not be protected.'); }
              await loadCare();
            })}
            onRemove={fact => void run(async () => {
              if (!api.removeProtectedFact) throw new Error('This server does not accept protected-fact removals.');
              setCareError('');
              try { await api.removeProtectedFact(fact.id); }
              catch (e) { setCareError(e instanceof Error ? e.message : 'That fact could not be removed.'); }
              await loadCare();
            })}
          />}
      </section>}

      {screen === 'agreements' && <Agreements
        agreements={agreements}
        timezone={today?.timezone ?? 'Europe/London'}
        busy={busy}
        unavailable={agreementsUnavailable}
        onCheck={() => void run(async () => { if (api.checkAgreements) setAgreements(await api.checkAgreements()); })}
        onOpen={openChange}
      />}

      {screen === 'skills' && <Skills
        skills={skills}
        change={change}
        changes={allChanges}
        timezone={today?.timezone ?? 'Europe/London'}
        disabled={busy || offline}
        owner={session?.authenticated_owner === true}
        onRevoke={name => void run(async () => { await api.revokeSkill(name); setSkills(await api.skills()); })}
        teach={<Teach disabled={busy || offline} owner={session?.authenticated_owner === true} onInstalled={() => void loadSkills()} />}
      />}

      {screen === 'limits' && <StandingLimits onSaved={value => { setLimitsMode(value.mode); setLimitCatalogue(value.clauses); setLimitsChosen(value.updated_at !== null); }} disabled={busy || offline} owner={session?.authenticated_owner === true} />}

      {screen === 'settings' && <section className="settings-page">
        <nav className="settings-tabs" aria-label="Settings sections">
          <button aria-current={settingsTab === 'connections' ? 'page' : undefined} onClick={() => setSettingsTab('connections')}>Connections</button>
          <button aria-current={settingsTab === 'preferences' ? 'page' : undefined} onClick={() => { setSettingsTab('preferences'); void run(async () => setPreferences(await api.preferences())); }}>Preferences</button>
          {sampleMode && <button aria-current={settingsTab === 'demo' ? 'page' : undefined} onClick={() => setSettingsTab('demo')}>Demo</button>}
        </nav>
        {settingsTab === 'connections' && <div className="settings-sheet">
          <h2>Connect only what this task needs.</h2>
          <p>In live mode, selected context goes to cloud inference and public venue queries go to search. Your private availability is kept separate.</p>
          {session?.connections.map(c => <div className="connection" key={c.service}>
            <div>
              <h3>{{ gmail: 'Gmail', calendar: 'Work Calendar', tavily: 'Public venue research', nemotron: 'Cloud planning' }[c.service]}</h3>
              <p>{c.service === 'gmail' ? 'Read the selected meeting thread; send only the exact approved reply.' : c.service === 'calendar' ? 'Read work meetings; update an owner-organized invite after approval.' : c.detail}</p>
              <small>{c.state} · {c.detail}</small>
            </div>
            <button disabled={busy || c.state === 'connected'} onClick={() => void run(async () => setSession(c.state === 'authorizing' ? await api.session() : await api.connect(c.service)))}>
              {c.state === 'expired' ? `Reconnect ${c.service}` : c.state === 'connected' ? 'Connected' : c.state === 'authorizing' ? 'Check authorization' : `Connect ${c.service}`}
            </button>
          </div>)}
          <form onSubmit={e => { e.preventDefault(); void run(async () => { setSession(await api.setupBoundary({ start: boundaryTime, buffer_minutes: buffer })); await refreshDay(); if (change) setChange(await api.change(change.id)); }); }}>
            <h2>Private availability</h2>
            <p>Enter a time, not a private event title. Only this planning fact may leave the device: “Unavailable from {boundaryTime}, with {buffer} minutes before it.”</p>
            <div className="form-row">
              <label>Protected start<input type="time" value={boundaryTime} onChange={e => setBoundaryTime(e.target.value)} required /></label>
              <label>Buffer in minutes<input type="number" min="0" max="240" value={buffer} onChange={e => setBuffer(Number(e.target.value))} required /></label>
            </div>
            <button disabled={busy}>Save protected boundary</button>
          </form>
          <div className="connection">
            <div><h2>Meeting changes routine</h2><p>Prepare relevant changes in the background. Sending and invite updates follow your approvals and standing limits.</p></div>
            <button disabled={busy} onClick={() => void run(async () => setSession(await api.routine(session?.routine.enabled ? !session.routine.paused : false)))}>
              {session?.routine.paused || !session?.routine.enabled ? 'Enable meeting changes' : 'Pause meeting changes'}
            </button>
          </div>
          {/* The owner credential, entered here and held in memory for this tab. It is the ONLY way
              the live client ever gets one: there is no build-time variable for it, so this field is
              not a convenience over a default — it is the mechanism. Nothing is written to storage,
              so closing the tab forgets it, and the token is sent only to a loopback base URL. */}
          {apiMode === 'live' && <form onSubmit={event => {
            event.preventDefault();
            setOwnerToken(tokenDraft);
            setTokenDraft('');
            setHasToken(ownerToken() !== null);
            void run(load);
          }}>
            <h2>Owner token</h2>
            <p>This build ships no credential of its own — nothing about you is in the files it served. Paste the token from <code>~/.privatemesh/owner.token</code> to act as the owner in this tab. It is kept in memory only: closing the tab forgets it, and it is never sent anywhere but this machine.</p>
            <label>Owner token<input type="password" autoComplete="off" value={tokenDraft} onChange={e => setTokenDraft(e.target.value)} /></label>
            <div className="button-row">
              <button className="primary" disabled={busy || !tokenDraft.trim()}>Use this token</button>
              <button type="button" disabled={busy || !hasToken} onClick={forgetOwnerToken}>Forget token</button>
            </div>
            <p className="secondary" role="status">{hasToken
              ? 'A token is held for this tab. Owner-only routes are readable.'
              : 'No token is held. Owner-only routes refuse, and this workspace reads as unavailable rather than as empty.'}</p>
          </form>}
          {sampleWorkspace && <p className="alert">These are simulated connections. No account authorization is requested in the sample.</p>}
        </div>}
        {settingsTab === 'preferences' && <div className="settings-sheet">
          <h2>Consented preferences</h2>
          <p>Only facts you approve can guide later plans. Revocation stops future use; it cannot erase data already transmitted. Action records are retained separately. Revoking a fact also forgets it from preferences; the record of an action already taken remains.</p>
          {preferences.length === 0 && <p>No preferences saved. Share only what is useful.</p>}
          {preferences.map(p => <div className="connection" key={p.id}>
            <div><h3>{p.fact}</h3><p>{p.scope} · Source: {p.source}</p><small>Last used: {p.last_used ?? 'Not yet'}</small></div>
            <div className="button-row">
              <button onClick={() => { setFact(p.fact); setScope(p.scope); setEditingPreference(p.id); }}>Edit preference</button>
              <button disabled={busy} onClick={() => void run(async () => { await api.deletePreference(p.id); setPreferences(await api.preferences()); })}>Revoke</button>
            </div>
          </div>)}
          <form onSubmit={e => { e.preventDefault(); void run(async () => { if (editingPreference) await api.updatePreference(editingPreference, { fact, scope }); else await api.savePreference({ fact, scope, source: 'Owner entered' }); setEditingPreference(null); setPreferences(await api.preferences()); setFact(''); }); }}>
            <h2>{editingPreference ? 'Edit preference' : 'Add a preference'}</h2>
            <label>Fact<input value={fact} onChange={e => setFact(e.target.value)} required maxLength={300} /></label>
            <label>Scope<input value={scope} onChange={e => setScope(e.target.value)} required /></label>
            <button className="primary" disabled={busy}>Consent and save</button>
          </form>
          <details><summary>Suggested preference · not yet used</summary>
            <p>Rosa does better with morning appointments.</p>
            <button onClick={() => { setEditingPreference(null); setFact('Rosa does better with morning appointments.'); setScope('Meeting planning'); }}>Review suggestion</button>
          </details>
        </div>}
        {settingsTab === 'demo' && sampleMode && <div className="settings-sheet">
          <h2>Explore sample situations</h2>
          <p>This whole workspace is synthetic. Switch scenarios to see how the product handles a partial send, an expired approval, a disconnected tool, or a limit handing a change back to you — the one launcher for every demo situation, so the rest of the product never has to mention it again.</p>
          <label>Scenario<select value={variant} onChange={e => void reset(e.target.value as SampleVariant)}>{variants.map(v => <option key={v} value={v}>{variantNames[v]}</option>)}</select></label>
          <p className="secondary">Switching resets this synthetic workspace and returns you to Today.</p>
        </div>}
      </section>}

      {screen === 'why' && <section className="settings-sheet">
        <button onClick={() => setScreen('change')}>Back to the plan</button>
        {why ? <>
          <h2>Used</h2>
          {why.used.map((x, index) => <div className="record" key={`${x.label}-${index}`}><strong>{x.label}</strong><p>{x.detail}</p>{x.evidence && <Source evidence={x.evidence} />}</div>)}
          <OverreachList events={why.overreach} />
          <h2>Kept private</h2>
          <p>Private categories, never their contents.</p>
          <ul>{why.kept_private.categories.map(c => <li key={c}>{c} {why.kept_private.counts[c] !== undefined && `· ${why.kept_private.counts[c]} excluded`}</li>)}</ul>
          <h2>Changed</h2>
          <ul>{why.changed.map(c => <li key={c}>{c}</li>)}</ul>
          {why.effects?.map(effect => <details key={effect.kind}>
            <summary>{effect.kind === 'calendar_update' ? 'Calendar fields' : 'Reply fields'} · {effect.state}</summary>
            <dl className="diff">{Object.entries(effect.fields).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{Array.isArray(value) ? value.join(', ') : value}</dd></div>)}</dl>
            {effect.provider_id && <p>Provider record: {effect.provider_id}</p>}
            {safeHttpUrl(effect.provider_url) && <a href={safeHttpUrl(effect.provider_url)!} target="_blank" rel="noreferrer">Open provider record</a>}
          </details>)}
          <details><summary>Technical record</summary>
            <p>Model calls recorded: {why.technical.model_calls}. A recorded request is not a provider-confirmed response.</p>
            <ul>{why.technical.model_call_records?.map(call => <li key={call.id}>{call.model}: {call.status === 'provider_confirmed' ? 'Provider-confirmed response' : 'Attempted request; response not confirmed'}</li>)}</ul>
            <pre>{JSON.stringify(why.technical, null, 2)}</pre>
          </details>
        </> : <p>Loading the evidence…</p>}
      </section>}
    </div>
  </Shell>;
}
