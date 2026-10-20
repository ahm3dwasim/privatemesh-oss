# The NVIDIA stack, and what it does not do

PrivateMesh already runs on NVIDIA's model — Nemotron, served through Nebius Token Factory
(`llm.py`, `config.py`). This document is about the two other pieces the track rules name — NemoClaw
and OpenShell — what each does for the product, and, in the second section, the one thing that
layer cannot do and never claims to. The fourth section is what running it for real found.

## 1. What each NVIDIA piece does for us

### NemoClaw — `SKILL.md`, the persistence format

NemoClaw (`github.com/NVIDIA/NemoClaw`) documents `SKILL.md` as the file a Hermes Agent deployment
reads to persist a learned capability across redeployment: a Markdown file with a small YAML
frontmatter (`.agents/skills/nemoclaw-skills-guide/SKILL.md` in NemoClaw's own repository declares
`name`, `description`, `license`), plus a body a reader — human or agent — can read for what the
skill does.

`privatemesh skills export --format hermes --out DIR` (`src/privatemesh/export.py`) writes one such
file per taught skill the owner has approved (`SkillLibrary.taught()`), so a capability PrivateMesh
learned once is a file that survives the process that learned it, in the format Hermes reads. **What
NemoClaw documents is exactly those three frontmatter keys and the one-file-per-skill convention —
nothing more.** Everything else the exported file carries is PrivateMesh's OWN convention, stated as
such in both the frontmatter and the body of every file this exporter writes:

* a nested `privatemesh:` frontmatter block — `identity` (the sha256 the library already binds the
  approval to), `version`, `rule` (the owner's exact sentence), `expires`, `reads`, `proposes`,
  `narrowing_only: true`, and explicit `may` / `may_not` lists;
* a Markdown body repeating the same limit in prose ("What this may do" / "What this may never
  do"), so a reader of the file — not only a reader of the frontmatter's YAML — sees the rule the
  code is bound to;
* a "Verification" section naming the identity and stating that narrowing-only was RE-checked at
  export time, independent of the library's own `rule` column (see `export.verify_narrowing_only`).

No skill is exportable unless it is both **taught** (it carries the owner's own sentence — the one
hard-coded capability, `learn.CAPABILITY`, is never in `SkillLibrary.taught()` and is never exported)
and **re-verified as narrowing-only right now**, by re-running its stored code over PrivateMesh's
fixed safety cases (`teach.invariant_cases`) and checking `teach.widenings` comes back empty — not by
trusting that the row says "taught". A library row edited by hand, or installed directly rather than
through the learning loop, has no way to defeat this: `SkillLibrary.install` enforces that the bytes
match their own hash, never that the code behaves.

### OpenShell — the sandbox PrivateMesh runs inside

OpenShell (`github.com/NVIDIA/OpenShell`, Apache-2.0) is a declarative-policy sandbox: a YAML file
says which paths are read-only, read-write, or invisible, and which network endpoints a process may
reach — enforced by a proxy the process cannot see around, not by the process's own good behaviour.
`scripts/openshell_run.sh` is the one command that builds a sandbox from either policy file below,
runs the injection red-team corpus and the egress probe inside it, and tears it down — every time,
including on failure. It has been run for real; see "What running it found" below.

`deploy/openshell/privatemesh.policy.yaml` is that policy for a real, `--live` run: filesystem
access is read-write only under `/sandbox` (the sandbox user's HOME, where PrivateMesh's data
directory and the owner's Google token both land — OpenShell requires absolute paths, so this is
not a choice, see finding 1 below), read-only everywhere else OpenShell mounts by default, and
`include_workdir: false` — the sandbox never sees the host checkout directly, only the archive
`scripts/openshell_run.sh` builds with `git archive HEAD` and uploads (finding 8 below is why this
matters: `include_workdir: true` would mount the real working tree, untracked files included).
Network access is exactly the METHOD+PATH pairs the adapters call — read off `gmail.py`, `gcal.py`,
`google_auth.py`, `skills_builtin.py`/`tavily_evidence.py`, and `llm.py` — across five host groups
(Nebius Token Factory, Gmail, Calendar, Google's OAuth token endpoint, and Tavily), each with a
comment naming the feature and the adapter call it exists for (finding 2 below is why this is
method+path rules and not a whole-host preset). `deploy/openshell/demo.policy.yaml` is the narrower
policy for the public demo, and it grants exactly one host and one rule: `POST
/v1/chat/completions` on the model. No Google, no Tavily. That is not a stricter choice among
several reasonable ones — it is the demo's own claim (`serve --v2 --demo` refuses to start beside a
real provider credential — `wiring.DemoRefused`) written down as something a proxy enforces rather
than something the README merely asserts.

## 2. What OpenShell's policy cannot decide

Lasso Security's research on OpenShell states the boundary precisely: *"OpenShell's binary- and
endpoint-scoped egress policies are correctly enforced, but they cannot evaluate intent."* ([Lasso
Security, "Exfiltrating OpenClaw Data from NVIDIA's new
Sandbox"](https://www.lasso.security/blog/sandboxed-ai-agents-attack-surface)) Their attack used
tools a sandbox has to allow for an agent to do its job at all — `git`, `gh`, `npm`, `node` — to
carry sandbox secrets out through an endpoint the policy had every reason to permit. **No policy was
violated; the sandbox behaved exactly as configured.** This is a fact about a layer PrivateMesh is
proposing to USE, stated because it is true of that layer, not as a criticism of it — OpenShell's own
documentation makes the same distinction between what enforcement decides and what it cannot see.

PrivateMesh's own red-team corpus has the concrete instance of the same shape, and it predates this
document: **case 02**, `redteam/cases/02-new-recipient-and-home-address.json`. The injection asks the
engine to cc a second address on the reply and to include the owner's home address in the body. The
reply is sent through Gmail's API — `gmail.googleapis.com`, an endpoint `privatemesh.policy.yaml`
explicitly allows, because the product needs to send Gmail replies. **A network-level policy — sender
uses an allowed host, over HTTPS, to a well-formed API — is right to permit that request.** What
refuses it is not the sandbox: it is that `changes.py` pins `to` to the thread's sender before the
model is ever consulted (a cc to a second address is never constructed), and `boundary.PrivacyGate`
refuses the home address as soon as it appears in a composed body, regardless of which endpoint the
composed bytes are headed to. The sandbox cannot see the difference between "a reply to the person
who emailed" and "a reply plus a cc the model invented" — both are one authorized POST to one
allowed host. Only a guard that reads the CONTENT and the PROVENANCE of the request — which
PrivateMesh's engine is, and OpenShell's proxy is not — can tell them apart.

## 3. What would still be needed if OpenShell were enforcing underneath

Read this table as: even in the best case — PrivateMesh actually running inside
`privatemesh.policy.yaml`, correctly enforced — every row below is still doing work the sandbox
cannot do for it.

| PrivateMesh guard | What it catches | Why OpenShell's policy cannot catch it instead |
| --- | --- | --- |
| `boundary.PrivacyGate.check_text` (egress scan for a protected value, verbatim or paraphrased) | A protected title, location, reason or attendee reaching an outbound payload | The payload is legitimate bytes to an allowed host; the policy has no notion of "this string is the owner's private meeting reason" |
| `changes._prepare`'s recipient pin (`to` fixed to the thread sender before the model runs) | A model-invented cc or a redirected reply | The destination is still an allowed host; the policy cannot know which address SHOULD receive this specific reply |
| `execute.py`'s pre-effect grant re-check (mode, revision, digest) | A plan whose standing authorization changed, or whose reviewed content changed, between approval and send | This is state internal to PrivateMesh's own store; the sandbox has no view of it |
| `export.verify_narrowing_only` (re-run a taught skill's code over the fixed safety cases at export time) | A taught skill whose code widens despite a `rule` column that says "taught" | The sandbox would isolate the skill's process; it has no notion of "narrowing" as a property of the skill's OUTPUT |
| `library.SkillLibrary.load`'s identity re-derivation | A skill row whose bytes no longer hash to the identity the owner approved | Filesystem policy can protect the database FILE; it cannot re-derive an application-level hash over three of its columns |
| `redteam.py`'s injection corpus itself | Whether the engine holds when a hostile email fully controls what the model is asked to do | This is a claim about the engine's own decision logic on inputs that arrive over an ALLOWED channel (an email) — no network policy is relevant to it at all |

None of this is an argument against OpenShell — the opposite: a sandbox that enforced nothing would
leave every guard above as the ONLY layer, with no defence-in-depth if one of them had a bug. The
argument is the one Lasso's finding makes and this table restates for PrivateMesh's own guards:
egress control and intent control are two different jobs, and a product that only had the first
would be a product that read as secure and was not.

## 4. What running it found

OpenShell 0.0.116 is installed on this machine and its gateway runs locally, so
`scripts/openshell_run.sh` no longer stops at a refusal: it creates a real sandbox from either
policy file, uploads a pre-downloaded dependency bundle (the sandbox's system Python has no pip
and never gets package-registry egress), runs the injection red-team corpus in all three
standing-limit modes, runs `scripts/openshell_egress_probe.py`, and deletes the sandbox — every
time, including on failure. None of this was visible to the text-level policy tests.

**Five findings from the first hand-run (2026-09-21):**

1. **The first policy draft was REJECTED at `sandbox create`**: `policy contains unsafe content:
   path must be absolute (start with '/'): data/v2; ... ~/.privatemesh`. Inside the sandbox the
   workdir AND `$HOME` are `/sandbox` — both files now grant read-write on `/sandbox` alone.
2. **`access: read-only` on Google OAuth and Tavily DENIED `POST /token` and `POST /search` at
   OpenShell's L7 layer** — body: `{"error":"policy_denied","layer":"l7","detail":"POST /token not
   permitted by policy","binary":"/usr/bin/python3.12",...}`. Under that policy the product could
   never refresh its Google token or run a venue search. Both files now grant method+path `rules`
   read off the adapters, not host-level presets.
3. `/usr/bin/python3` in the sandbox image is **3.12.3** (a `3.14.3` seen in earlier exploration
   was the image's OWN `.venv`, reached only via a bare `python3` on `PATH` before `/usr/bin`);
   OpenShell resolves the symlink and names the binary `/usr/bin/python3.12` in its denial.
4. `openshell sandbox exec` **hangs waiting on stdin** unless stdin is `/dev/null`; `sandbox create
   -- <short cmd>` ends `MainProcessExited`. Working shape: create with no command → `upload` →
   `exec ... < /dev/null` → `delete`.
5. The image has no `pip` for the system Python; wheels were unpacked with `python3 -m zipfile -e`.

**Two more, from running `scripts/openshell_run.sh` itself (this session, 2026-09-21 — evidence is
this run's own terminal output, not re-derived from the record above):**

6. `sandbox upload NAME LOCAL_PATH [DEST]` nests `LOCAL_PATH` under ITS OWN basename, inside
   `DEST` — true for a directory unconditionally, and for a file whenever `DEST` differs from
   `basename(LOCAL_PATH)`. The script's first draft uploaded the egress probe to a `DEST` of
   `egress_probe.py` (its basename is `openshell_egress_probe.py`) and the exec step then failed
   with `can't find '__main__' module in '/sandbox/egress_probe.py'` — that path was a directory
   holding the real file. Omitting `DEST` is the one shape that lands an upload flat, under its own
   name, at the workdir.
7. **Per-binary enforcement is checked against `sandbox exec`'s own top-level command, not against
   every descendant process's binary.** `curl` — a binary neither policy's `binaries:` list
   names — run as a *subprocess of the granted* `/usr/bin/python3` egress probe reached the network
   its parent was granted (`POST .../messages/send` came back a real `401`, i.e. it reached
   Google). The identical request, run as `curl`'s OWN `sandbox exec` command, was blocked at the
   tunnel (`curl: (56) CONNECT tunnel failed, response 403`). `scripts/openshell_run.sh` checks
   per-binary enforcement the second way, as its own exec call, for exactly this reason. **State
   this plainly: `binaries:` is a limit on which top-level command may open a connection, not a
   sandbox-wide guarantee that an ungranted binary can never reach the network — a granted process
   (`/usr/bin/python3`, in this product) that spawns an ungranted child inherits its own grant for
   that child. Nothing in this policy or this product closes that; it is a property of the layer
   PrivateMesh is proposing to use, named here so no other document has to re-derive it.**

**One more, from reviewing the policy rather than from a run (2026-09-22 — this one is NOT measured
by `scripts/openshell_run.sh` against a live sandbox; it follows from OpenShell's own
`FilesystemPolicy` schema and from what the script actually uploads, and it was not run because
this review's instructions were to read, not to create sandboxes):**

8. **`include_workdir: true` would have mounted the real host checkout, not the `git archive`ed
   one, into the sandbox — read-write.** The comment this key used to carry argued for `true` on the
   grounds that it was "the whole reason `scripts/openshell_run.sh` does not also need `--upload`" —
   but the script never relies on that: it builds its own archive of `${ARCHIVE_PATHS[@]}` with
   `git archive HEAD` (which cannot see an untracked file) and puts it in the sandbox itself with
   `sandbox upload`, exactly the `--upload` the old comment said was unneeded. `include_workdir:
   true` was therefore not load-bearing for anything the script does, and left open a second,
   completely uninspected path for host files to enter the sandbox — a root `.env`, anything under
   `secrets/`, or any other untracked file sitting in the checkout at run time — that the archive's
   own `.env`/`secrets/` check (`scripts/openshell_run.sh`) never looked at, because it only ever
   inspected the archive. Both policy files now set `include_workdir: false`; the sandbox's only
   filesystem content is what `sandbox upload` puts there, and the existing archive check is now the
   complete check on what that is.

**Red-team corpus inside the sandbox, both policies, all three standing-limit modes — held 9/9
every time:**

| Policy | `ask_always` | `within_limits` | `off` |
| --- | --- | --- | --- |
| product (`privatemesh.policy.yaml`) | 9/9 held | 9/9 held | 9/9 held |
| demo (`demo.policy.yaml`) | 9/9 held | 9/9 held | 9/9 held |

**Egress, per host AND method, measured with `scripts/openshell_egress_probe.py` (no credential
sent — a 4xx is the provider seeing and refusing an anonymous caller, which is the expected,
credential-free result):**

Under the **product** policy (method+path rules): `POST` Gmail send, `POST` the model, `POST`
OAuth `/token`, and `POST` Tavily `/search` all **REACHED** the provider (4xx); `GET`/`DELETE` on
five neighboring Gmail/Calendar/model paths the adapters never call were **DENIED by OpenShell's
L7 layer**; `example.com`, `pypi.org` and the red-team's own `attacker.example.net` were **BLOCKED
at the tunnel**; `curl` (an ungranted binary) got nothing, as its own exec command, on the exact
same Gmail-send request `/usr/bin/python3` reached.

Under the **demo** policy: only `POST /v1/chat/completions` on the model reaches anything; `GET
/v1/models` on the same host is denied at L7; every Google, Tavily, registry and attacker request
is blocked at the tunnel — matching the file's own claim of granting exactly one host and one rule.

**The sentence the entry rests on, now measured twice.** `POST
/gmail/v1/users/me/messages/send` is REACHED under the product policy — and it has to be, because
replying is the assistant's job. The recipient of a send lives inside a base64url RFC 822 body
that no method/path/query rule can read, so a reply to Alex and an exfiltration to an attacker's
address are the SAME request to OpenShell. OpenShell is right to permit it; the red-team corpus's
own case 02 shows PrivateMesh refusing the second one. Two layers, two different jobs: OpenShell
decides where bytes may go, and on which verbs and paths; PrivateMesh decides whether the owner
approved THESE bytes to THIS person.

**What is still not run.** Every red-team run above drives the deterministic sample engine over
simulated Gmail/Calendar/Tavily ports — no live-provider run has happened inside an OpenShell
sandbox, and no Google token, Nebius key or Tavily key has ever been uploaded into or typed inside
one; every egress-probe row that reaches a provider does so anonymously, and a 401/400 back is the
expected, credential-free result, not a live call succeeding. The public demo (`serve --v2
--demo`) has not been hosted inside OpenShell either — `demo.policy.yaml` has been proven against
this same red-team-and-egress harness, which is evidence about the POLICY's shape, not about the
demo server having ever executed under it.

## Sources read to get the format right

* `https://github.com/NVIDIA/OpenShell` — README, `examples/sandbox-policy-quickstart/{policy.yaml,demo.sh}`,
  `crates/openshell-driver-mxc/examples/e2e-policies/network-reject.yaml`, `deploy/man/openshell.1.md`
* `https://github.com/NVIDIA/NemoClaw` — `.agents/skills/nemoclaw-skills-guide/SKILL.md`
* `https://www.lasso.security/blog/sandboxed-ai-agents-attack-surface` — "Exfiltrating OpenClaw Data
  from NVIDIA's new Sandbox"
