"""`privatemesh models` lists visible models; `ping` makes one FAST-tier call; `smoke` runs three
live turns end to end; `serve` runs the API, `serve --v2 [--sample|--live]` runs the v2
product; `google-connect` runs the OAuth consent flow."""
from __future__ import annotations

import sys
from pathlib import Path

from .config import Settings
from .llm import Tier, TokenFactory

#: v2 modes, and what a bare `--v2` means. Sample is the default because it is the mode that
#: cannot fail on a machine with no credentials — a first run should show the product, not an
#: error about a key.
#:
#: `demo` is the third mode and the one a STRANGER runs: real model, simulated providers, hard
#: caps, and no credential of the owner's anywhere in the process. It is spelled `--v2 --demo`
#: rather than reusing the bare `--demo` that already exists, and that is not fussiness: bare
#: `--demo` is the older v1 simulated app, its port and its `data/demo` directory are documented
#: and relied on, and quietly re-pointing that flag at a different product would be a rename
#: disguised as a feature.
V2_MODES = ("sample", "live", "demo")

#: `serve`'s grammar, in one place. `--flag` entries take no value; `--flag=` style is not
#: supported and is therefore refused rather than half-understood.
SERVE_SWITCHES = ("--v2", "--sample", "--live", "--demo", "--owner-session")
SERVE_VALUED = ("--host", "--port")

SERVE_USAGE = ("usage: privatemesh serve [--demo] [--v2 [--sample|--live|--demo]] "
               "[--owner-session] [--host HOST] [--port PORT] [host] [port]")


class ServeUsage(Exception):
    """A `serve` invocation this program will not guess at."""


def serve_bind(argv: list[str]) -> tuple[str, int]:
    """(host, port) for `serve`, or `ServeUsage`. Never a guess.

    The parser this replaces kept every token that did not start with `--` and read the first two
    as host and port, which made `serve --v2 --demo --port 8799` bind HOST `8799` and PORT 8765:
    `--port` was dropped as a flag, its value became the host, and `8799` is a legal integer
    literal that `getaddrinfo` resolves to `0.0.34.95`. The server came up on an address nobody
    asked for and reported a port nobody set, which is the kind of failure that looks like a
    network problem for an afternoon.

    So: flags a caller may use are listed, `--host`/`--port` are the supported way to name either,
    the two positionals keep working in the order the usage line has always printed them, and
    anything else is refused with a message. A bare-number positional is refused too — it is
    either a port whose flag was forgotten or a hostname nobody should type, and both are better
    said out loud than resolved to an address."""
    rest = list(argv)
    host: str | None = None
    port: int | None = None
    positional: list[str] = []
    while rest:
        arg = rest.pop(0)
        if arg in SERVE_SWITCHES:
            continue
        if arg in SERVE_VALUED:
            if not rest:
                raise ServeUsage(f"{arg} needs a value")
            value = rest.pop(0)
            if arg == "--host":
                if not value.strip():
                    raise ServeUsage("--host needs a non-empty address")
                host = value
            else:
                port = _serve_port(value)
            continue
        if arg.startswith("--"):
            raise ServeUsage(f"unexpected argument {arg!r}")
        positional.append(arg)
    if len(positional) > 2:
        raise ServeUsage(f"too many positional arguments: {' '.join(positional)}")
    if positional:
        if positional[0].isdigit():
            # `8799` as a host is `0.0.34.95` after `getaddrinfo`. This is the flag that was
            # dropped, nine times out of ten.
            raise ServeUsage(
                f"expected a HOST, got the number {positional[0]!r} — if that is a port, write "
                f"`--port {positional[0]}`; the positional order is `[host] [port]`")
        if host is None:
            host = positional[0]
    if len(positional) > 1:
        if port is not None:
            raise ServeUsage("a port was given twice, as `--port` and as the second positional")
        port = _serve_port(positional[1])
    return host or "127.0.0.1", port


def _serve_port(raw: str) -> int:
    """One port, or a refusal. 1–65535: 0 asks the kernel for a port nobody can be told about."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        # `from None`: the `ValueError` is this function's own input check, not a cause a reader of
        # the refusal needs chained above it.
        raise ServeUsage(f"expected a port number, got {raw!r}") from None
    if not (1 <= value <= 65535):
        raise ServeUsage(f"port {value} is outside 1-65535")
    return value


def _v2_mode(args: list[str]) -> str | None:
    """The requested v2 mode, or None when v2 was not asked for. `--live` wins over the rest.

    `--demo` counts only beside `--v2`. On its own it is the v1 flag it has always been, which is
    why the test that pins `serve --demo`'s port still describes the product after this change."""
    if "--live" in args:
        return "live"
    if "--v2" in args and "--demo" in args:
        return "demo"
    if "--v2" in args or "--sample" in args:
        return "sample"
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "help"
    if cmd == "serve":
        import uvicorn

        from .api import default_app

        v2_mode = _v2_mode(argv[1:])
        # Parsed BEFORE anything is built. A usage error must not create a data directory, mint a
        # token or open a store — an operator who typed the wrong flag should get a message, not a
        # half-started process to clean up.
        try:
            host, explicit_port = serve_bind(argv[1:])
        except ServeUsage as exc:
            print(f"serve: {exc}", file=sys.stderr)
            print(SERVE_USAGE, file=sys.stderr)
            return 2
        # `--v2 --sample` is the zero-credential path: the v2 half is simulated, so the v1 chat
        # half is the demo too — a "sample" that refuses to start without an API key is not a
        # sample. `--v2 --live` leaves v1 exactly as it was.
        demo = "--demo" in argv[1:] or v2_mode == "sample"
        service, wiring, settings = None, None, None
        if v2_mode is not None:
            from . import owner_token
            from . import wiring as wiring_module

            if v2_mode == "live" and "--owner-session" in argv[1:]:
                # The old flag, kept as the answer to the question it was really asking. It used
                # to ASSERT ownership, which is the defect: a claim the code makes about itself,
                # at startup, to nobody. Now it PRINTS where the credential is and stops — the
                # operator still has to read the file to use it. It runs before `Settings.load()`
                # so that "where is my token" is answerable on a box with no credentials yet.
                token_path = owner_token.path()
                owner_token.issue(token_path)
                print(f"owner token: {token_path}", flush=True)
                print("Every owner-only route needs it as `Authorization: Bearer <token>`. It is "
                      "a credential: do not copy it into a deployed build, a log or a bug report.",
                      flush=True)
                return 0
            settings = Settings.load()
            # Demo mode is refused, not repaired: `build_demo` will not start in a process that
            # holds a real provider credential, and it will not start without a model key. Both
            # refusals are messages rather than tracebacks, and both arrive BEFORE the port is
            # bound — a demo that starts and then fails in front of a judge has already spent the
            # only thing it had.
            if v2_mode == "demo":
                try:
                    manager, wiring = wiring_module.build_demo(settings=settings)
                except wiring_module.DemoRefused as exc:
                    print("privateMesh v2 cannot start in demo mode:", file=sys.stderr)
                    print(f"  - {exc}", file=sys.stderr)
                    return 1
            # Sample mode needs nothing and is the default; live needs credentials and a
            # protected commitment, and the CLI is the one caller that REFUSES to start a live
            # product it cannot serve. The refusal is a message, not a traceback: an operator
            # reading "connect Google first" can act on it.
            elif v2_mode == "live":
                # Live keeps files, because there "the same email twice" is a real inbox. The
                # sample deliberately does not: a judge who restarts it must see the same change
                # waiting for them, and a persisted watcher cursor would have already read it.
                #
                # The process MINTS an owner token at startup (see `owner_token`) and prints where
                # it put it, which is what makes the approve route a credential rather than a claim
                # the code makes about itself.
                wiring = wiring_module.build_live(
                    settings=settings, data_dir=Path("data") / "v2")
            else:
                wiring = wiring_module.build_sample(settings=settings)
            if v2_mode == "live" and not wiring.ready:
                print("privateMesh v2 cannot start in live mode:", file=sys.stderr)
                for reason in wiring.refusals:
                    print(f"  - {reason}", file=sys.stderr)
                print("Nothing was sent. Run with --sample to preview without connecting, or "
                      "fix the above and try again.", file=sys.stderr)
                return 1
            wiring.start()
            service = wiring.service
        # Demo and v2 runs share 8765, which is the port the web app and the local preview launch
        # config expect; a bare v1 run keeps 8000. An explicit `--port` (or the second positional)
        # wins.
        port = explicit_port if explicit_port is not None else (8765 if (demo or v2_mode) else 8000)
        tag = f" [v2 {v2_mode}]" if v2_mode else (" [demo]" if demo else "")
        # flush: this line and the rows below are what an operator reads in a log, and they sit
        # in a pipe's buffer forever if uvicorn takes over the process without them being written.
        print(f"PrivateMesh{tag} on http://{host}:{port}  (docs at /docs)", flush=True)
        if v2_mode == "demo":
            # The same three facts every demo SCREEN has to carry, printed here because this is
            # the demo's first screen and the operator is its first reader. A run that says only
            # "listening on 8765" leaves the one question a judge will ask — which parts of this
            # are real? — to be answered by a README, and the README is what the demo replaces.
            caps = manager.caps
            print(f"  REAL: the model (Nebius Token Factory) and the caps, which are enforced "
                  f"in code before each request.", flush=True)
            print(f"  SIMULATED: the inbox, the calendar, the web search — there is no provider "
                  f"credential in this process, and it would refuse to start if there were.",
                  flush=True)
            print(f"  PUBLIC: /v2/demo/session and its three siblings need no token. Every other "
                  f"/v2 route refuses.", flush=True)
            print(f"  CAPS: ${caps.session_usd}/session, {caps.session_calls} calls/session, "
                  f"{caps.rate_requests} requests/{int(caps.rate_window)}s per client, "
                  f"${caps.daily_usd}/day, ${caps.total_usd} total.", flush=True)
            print("  A visitor who hits a cap is told so and gets the scripted answer; nothing "
                  "here can overspend to serve them.", flush=True)
        if wiring is not None and wiring.owner_token:
            # The operator has to be TOLD where the credential is; a gate nobody can open is an
            # outage. The path, never the token: this line goes to a terminal and a log.
            print(f"  owner token: {owner_token.path()} "
                  f"(send it as `Authorization: Bearer <token>`; owner-only routes refuse "
                  f"without it)", flush=True)
        for row in (wiring.connections if wiring else []):
            print(f"  {row['service']}: {row['state']} — {row['detail'][:110]}", flush=True)
        try:
            uvicorn.run(default_app(settings, demo=demo, v2=service), host=host, port=port)
        finally:
            if wiring is not None:
                wiring.stop()
        return 0
    if cmd == "models":
        for m in TokenFactory(Settings.load()).models():
            print(m)
        return 0
    if cmd == "spend":
        from .spend import SpendLedger
        t = SpendLedger().totals()
        print(f"spent ${t['total_usd']:.4f} of ${t['cap_total_usd']:.2f} cap  |  "
              f"today ${t['today_usd']:.4f} of ${t['cap_daily_usd']:.2f} daily cap")
        return 0
    if cmd == "ping":
        # The cheapest possible proof that the key, the base URL and the tier models are wired:
        # FAST, 16 tokens, thinking off. Everything that can go wrong here is worth SEEING, so a
        # failure prints its cause and exits 1 rather than raising a traceback at the operator.
        try:
            tf = TokenFactory(Settings.load())
            model = tf.model_for(Tier.FAST)
            out = tf.chat([{"role": "user", "content": "Reply with the single word: ready"}],
                          tier=Tier.FAST, max_tokens=16)
            print(f"model:   {out.get('model') or model}")
            print(f"content: {out['choices'][0]['message']['content']}")
            print(f"usage:   {tf.usage}")
            return 0
        except Exception as exc:
            print(f"ping failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    if cmd == "redteam":
        return _redteam(argv[1:])
    if cmd == "smoke":
        from .smoke import main as smoke_main

        return smoke_main(argv[1:])
    if cmd == "google-connect":
        return _google_connect(argv[1:])
    if cmd == "skills":
        return _skills(argv[1:])
    print("usage: privatemesh models | ping | smoke | serve [--demo] [--host HOST] [--port PORT] "
          "[host] [port] "
          "| serve --v2 [--sample|--live|--demo] [--owner-session] [--host HOST] [--port PORT] "
          "[host] [port] "
          "| redteam [--live-model] [--mode ask_always|within_limits|off] "
          "| google-connect --account you|alex [--no-browser] "
          "| skills export --format hermes --out DIR\n"
          "  An unknown flag is refused rather than ignored, and the positional order is "
          "`[host] [port]`; `--port 8799` and `8799` as a HOST are not the same thing.\n"
          "  serve --v2 --live mints the owner token at ~/.privatemesh/owner.token (0600) and "
          "prints the path; every owner-only route needs it as `Authorization: Bearer <token>`. "
          "--owner-session prints that path and exits.")
    return 0 if cmd == "help" else 2


def _redteam(argv: list[str]) -> int:
    """`privatemesh redteam [--live-model] [--mode MODE]` — the injection corpus, through the real
    sample engine.

    The default run uses the sample's scripted model and reaches no network and no provider. The
    `--live-model` run spends real money against the configured Token Factory, which is why it
    prints the spend it is about to incur before it incurs it.

    `--mode` runs the corpus under one of the owner's standing presets. It changes who says yes, not
    who checks: `ask_always` (the default) is the owner path, where the harness presses approve on
    every proposal, so a refusal is the engine stopping a change the owner had already authorised.
    `within_limits` presses nothing, so a green means the standing yes did not cover the change.
    Both are real runs of the same guards, and the report records which one it was."""
    from . import limits as limit_rules
    from . import redteam

    live = None
    mode = None
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--live-model":
            live = True
        elif arg == "--mode":
            if not rest:
                print("usage: privatemesh redteam [--live-model] "
                      f"[--mode {'|'.join(limit_rules.MODES)}]", file=sys.stderr)
                return 2
            mode = rest.pop(0)
            if mode not in limit_rules.MODES:
                # Refused rather than defaulted, for the reason `limits.UnknownMode` gives: a typo
                # that quietly becomes `ask_always` reads as "the flag did nothing".
                print(f"unknown --mode {mode!r} — expected one of {', '.join(limit_rules.MODES)}",
                      file=sys.stderr)
                return 2
        else:
            print("usage: privatemesh redteam [--live-model] "
                  f"[--mode {'|'.join(limit_rules.MODES)}]", file=sys.stderr)
            return 2

    factory = None
    if live:
        from .config import Settings
        from .llm import TokenFactory
        from .spend import SpendLedger

        settings = Settings.load()
        if not settings.api_key:
            print("redteam --live-model needs NEBIUS_API_KEY; the deterministic run needs "
                  "nothing. Nothing was sent.", file=sys.stderr)
            return 1
        ledger = SpendLedger()
        totals = ledger.totals()
        print(f"live-model run against {settings.model_reason}; "
              f"spend so far ${totals['total_usd']:.4f} of ${totals['cap_total_usd']:.2f} cap",
              flush=True)
        factory = TokenFactory(settings, audit=None, spend=ledger)

    report = redteam.run(live_model=factory, limit_mode=mode)
    # The label has to say which of the three runs this was, because two of them print
    # `ask_always` and they are not the same run: the default presses approve on every proposal, and
    # `--mode ask_always` presses nothing and lets the standing answer decide — which, for
    # `ask_always`, is that nothing runs.
    label = (f"{report['limit_mode']}, no approval pressed" if report["limit_mode"]
             else "ask_always (owner path, approve pressed)")
    print(f"\ninjection red-team — {report['mode']} — {report['cases']} cases, "
          f"{report['held']} held — limits mode: {label}\n", flush=True)
    print(redteam.table(report["rows"]), flush=True)
    print(f"\nreport: {report['out_dir']}/report.json and report.md", flush=True)
    # Exit non-zero when a case failed. A red-team run is a check, and a check that always exits 0
    # is a check a pipeline cannot act on.
    return 0 if report["held"] == report["cases"] else 1


def _google_connect(argv: list[str]) -> int:
    """Print the consent URL, wait for the loopback redirect, store the token at 0600.

    Any safe account slug is accepted, not only the two demo identities: validating the name as a
    filename is the security boundary, and a third test account should not need a code change."""
    from . import google_auth

    account, open_browser, timeout = "you", True, 300.0
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--account" and rest:
            account = rest.pop(0)
        elif arg == "--no-browser":
            open_browser = False
        elif arg == "--timeout" and rest:
            try:
                timeout = float(rest.pop(0))
            except ValueError:
                print("--timeout needs a number of seconds", file=sys.stderr)
                return 2
        else:
            print(f"google-connect: unexpected argument {arg!r}\n"
                  f"usage: privatemesh google-connect --account you|alex [--no-browser] "
                  f"[--timeout seconds]", file=sys.stderr)
            return 2
    try:
        google_auth.validate_account(account)
    except ValueError as exc:
        print(f"google-connect: {exc}", file=sys.stderr)
        return 2
    try:
        return google_auth.connect(account, open_browser=open_browser, timeout=timeout)
    except google_auth.GoogleAuthError as exc:
        # The operator is standing at a terminal: say what happened and how to proceed, and exit
        # non-zero rather than raising a traceback at them.
        print(f"google-connect failed: {exc}", file=sys.stderr)
        print("Nothing was stored. Fix the cause and run it again.", file=sys.stderr)
        return 1


def _skills(argv: list[str]) -> int:
    """`privatemesh skills export --format hermes --out DIR` — the only subcommand today."""
    if not argv or argv[0] != "export":
        print("usage: privatemesh skills export --format hermes --out DIR", file=sys.stderr)
        return 2
    return _skills_export(argv[1:])


def _skills_export(argv: list[str]) -> int:
    from .boundary import BoundaryStore, EgressRefused, PrivacyGate
    from .care import ProtectedFactStore
    from .export import ExportRefused, export_taught_skills
    from .library import SkillLibrary

    fmt, out = None, None
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--format" and rest:
            fmt = rest.pop(0)
        elif arg == "--out" and rest:
            out = rest.pop(0)
        else:
            print(f"skills export: unexpected argument {arg!r}\n"
                  f"usage: privatemesh skills export --format hermes --out DIR", file=sys.stderr)
            return 2
    if fmt != "hermes":
        print(f"skills export: unsupported --format {fmt!r}; only 'hermes' is implemented today",
              file=sys.stderr)
        return 2
    if not out:
        print("skills export: --out DIR is required", file=sys.stderr)
        return 2

    # The same data directory `serve --v2 --live` reads and writes (`wiring.build_live`'s default),
    # so an export sees the library and the protected boundary the running product actually has —
    # never a second, drifted copy of either.
    data_dir = Path("data") / "v2"
    library = SkillLibrary(str(data_dir / "library.db"))
    boundary = BoundaryStore(path=str(data_dir / "boundary.db"))
    facts = ProtectedFactStore(path=str(data_dir / "facts.db"))
    gate = PrivacyGate(boundary, facts=facts)
    try:
        exported = export_taught_skills(library, out, gate=gate)
    except (ExportRefused, EgressRefused) as exc:
        print(f"skills export: {exc}", file=sys.stderr)
        return 1
    if not exported:
        print("no taught skills to export (the library has none active)", flush=True)
        return 0
    for skill in exported:
        print(f"wrote {skill.path}  ({skill.identity})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
