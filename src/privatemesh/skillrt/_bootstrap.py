"""The isolated child: arm the guard, then load the untrusted skill.

Run as ``python -I -S -B _bootstrap.py JOB_FD RESULT_FD``.

This file is deliberately NOT inside the `privatemesh` package and NOT inside the skill's
directory. Two reasons, and the second is the load-bearing one:

  * `-I -S` strips site-packages and the script directory from `sys.path`, so a package-relative
    import would not resolve;
  * the enforcement code must live outside the artifact it constrains. A generated skill cannot
    read this file (it is not under an allowed root) and cannot write it (writable roots are the
    scratch dir only).

The order below is the design, not an implementation detail:

  1. import everything the runtime itself needs — before the hook exists, so the runtime's own
     imports are not inspected and do not have to be allow-listed;
  2. apply the OS resource caps;
  3. read the job over an inherited descriptor (no `open`, so no audit event and no path to allow);
  4. install the audit hook;
  5. import the skill's module — the hook is ALREADY ARMED, so a hostile import dies here;
  6. call the entry point;
  7. write one JSON object to the inherited result descriptor.

Audit hooks cannot be uninstalled (CPython has no `removeaudithook`), so from step 4 onward the
skill cannot disarm the guard. That is the property the escape tests rest on.

What this is NOT: a VM boundary. It is a Python-level guard in the same process as the skill. It
stops the escapes it names (`violations.KINDS`) by intercepting CPython's own audit events, and it
is backed by an empty environment, an empty cwd, `-I -S`, and OS resource caps. It does not stop a
CPython interpreter bug, a C-level escape that fires no audit event, or a skill that forges the
result bytes on the descriptor it was handed. The parent re-validates every result against the
manifest and the broker independently re-decides every proposal, so a forgery can at most ask for
something the manifest already permits.
"""
from __future__ import annotations

import json
import os
import resource
import sys
import traceback

# ---------------------------------------------------------------- pure stdlib -----------------
# The standard library a PURE COMPUTATION needs, imported here — before the hook exists — so that
# they are already in `sys.modules` by the time a skill runs.
#
# This is not a convenience. A skill that does `import datetime` inside the child opens
# `.../python3.12/__pycache__/datetime.cpython-312.pyc`, which is a filesystem event outside the
# scratch directory, and the runtime stops it for a filesystem violation. Without this list a
# generated skill cannot parse a timestamp — which is to say, it cannot do the one job this
# runtime exists for.
#
# The list is a deliberate widening, so it is kept small and pure. NOT here, and each for a reason:
# `time` is a clock a pure function must not read, `random` is nondeterminism, `zoneinfo` reads
# TZif files off the disk, `socket`/`subprocess`/`ctypes` are refused by name. Everything below
# computes over values it is handed and reaches nothing else.
sys.dont_write_bytecode = True          # so the pre-import does not write .pyc into the stdlib
_PURE_STDLIB = ("bisect", "collections", "copy", "datetime", "decimal", "enum", "fractions",
                "functools", "heapq", "itertools", "math", "operator", "re", "statistics", "string")
for _module in _PURE_STDLIB:
    __import__(_module)
del _module, _PURE_STDLIB

# ------------------------------------------------------------------ the vocabulary ------------
# Mirrors `privatemesh.skillrt.violations.KINDS`; the two are kept in step by a test, because this
# file must stay importable with no package on sys.path.
KINDS = ("network", "subprocess", "process_control", "filesystem", "import", "environment",
         "memory", "resources")


class SkillViolation(RuntimeError):
    def __init__(self, kind: str, detail: str, *, event: str = "") -> None:
        self.kind = kind
        self.detail = detail
        self.event = event
        super().__init__(f"{kind}: {detail}" + (f" [{event}]" if event else ""))

    def as_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "event": self.event}


# ------------------------------------------------------------------ what is refused -----------
#: Audit event -> (kind, human sentence). Names verified against CPython 3.12 by probing each
#: vector directly (see the lane report): e.g. `socket.connect` fires for connect(),
#: `os.fork` for fork(), `subprocess.Popen` for subprocess.run().
DENY_EVENTS = {
    "socket.__new__": ("network", "opening a socket"),
    "socket.connect": ("network", "connecting a socket"),
    "socket.bind": ("network", "binding a socket"),
    "socket.getaddrinfo": ("network", "resolving a host name"),
    "socket.gethostbyname": ("network", "resolving a host name"),
    "socket.sendto": ("network", "sending a datagram"),
    "subprocess.Popen": ("subprocess", "starting a subprocess"),
    "os.system": ("subprocess", "os.system()"),
    "os.fork": ("process_control", "os.fork()"),
    "os.forkpty": ("process_control", "os.forkpty()"),
    "os.exec": ("process_control", "os.exec*()"),
    "os.posix_spawn": ("process_control", "os.posix_spawn()"),
    "os.spawn": ("process_control", "os.spawn*()"),
    "os.putenv": ("environment", "setting an environment variable"),
    "os.unsetenv": ("environment", "unsetting an environment variable"),
    "ctypes.dlopen": ("import", "loading a native library"),
    "ctypes.dlsym": ("import", "resolving a symbol from a native library"),
    "ctypes.call_function": ("import", "calling a native function"),
    "urllib.Request": ("network", "making a urllib request"),
    "http.client.connect": ("network", "opening an HTTP connection"),
    "ftplib.connect": ("network", "opening an FTP connection"),
    "smtplib.connect": ("network", "opening an SMTP connection"),
    "builtins.breakpoint": ("subprocess", "entering the debugger"),
}

#: Import roots that are refused outright. `_socket`/`_ssl` cover the C accelerators; `ssl` and
#: `socket` cover the Python wrappers; `urllib`/`http` cover the higher-level clients; `requests`
#: and `httpx` are the ones a generated skill reaches for first.
DENY_MODULES = frozenset({
    "ctypes", "_ctypes", "socket", "_socket", "ssl", "_ssl", "urllib", "http", "requests",
    "httpx", "urllib3", "ftplib", "smtplib", "telnetlib", "xmlrpc", "asyncio", "multiprocessing",
    "subprocess", "pty", "ctypes.util", "webbrowser",
})

#: Path-carrying audit events that are checked against the allowed roots.
PATH_EVENTS = {
    "os.listdir": ("filesystem", "listing a directory"),
    "os.scandir": ("filesystem", "scanning a directory"),
    "os.mkdir": ("filesystem", "creating a directory"),
    "os.rmdir": ("filesystem", "removing a directory"),
    "os.remove": ("filesystem", "removing a file"),
    "os.rename": ("filesystem", "renaming a file"),
    "os.link": ("filesystem", "creating a hard link"),
    "os.symlink": ("filesystem", "creating a symlink"),
    "os.chmod": ("filesystem", "changing file permissions"),
    "os.truncate": ("filesystem", "truncating a file"),
}

#: Mutable so the harness can revoke the held-out tests' root the instant they are imported.
STATE: dict = {"armed": False, "read_roots": [], "write_roots": []}


def _within(path: str, root: str) -> bool:
    try:
        real, real_root = os.path.realpath(path), os.path.realpath(root)
    except Exception:
        return False
    return real == real_root or real.startswith(real_root + os.sep)


def _in_any(path: str, roots) -> bool:
    return any(_within(path, r) for r in roots)


def _check_open(args) -> None:
    path, mode, flags = (list(args) + [None, None, None])[:3]
    if isinstance(path, int):
        raise SkillViolation("filesystem", f"opening raw file descriptor {path}", event="open")
    if not isinstance(path, str):
        return
    writing = False
    if isinstance(mode, str):
        writing = any(c in mode for c in "wax+")
    if not writing and isinstance(flags, int):
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
    roots = STATE["write_roots"] if writing else STATE["read_roots"]
    if not _in_any(path, roots):
        where = "writing" if writing else "reading"
        raise SkillViolation("filesystem", f"{where} {path!r} outside the scratch directory",
                             event="open")


def _check_path_event(event, args) -> None:
    kind, what = PATH_EVENTS[event]
    path = args[0] if args else None
    if not isinstance(path, str):
        return
    roots = STATE["write_roots"] if event in ("os.mkdir", "os.rmdir", "os.remove", "os.rename",
                                              "os.link", "os.symlink", "os.chmod",
                                              "os.truncate") else STATE["read_roots"]
    if not _in_any(path, roots):
        raise SkillViolation(kind, f"{what}: {path!r} outside the scratch directory", event=event)


def _hook(event, args) -> None:
    if not STATE["armed"]:
        return
    hit = DENY_EVENTS.get(event)
    if hit is not None:
        raise SkillViolation(hit[0], hit[1], event=event)
    if event == "import":
        name = (args[0] if args else "") or ""
        if name.split(".")[0] in DENY_MODULES:
            raise SkillViolation("import", f"import of {name!r} is not permitted", event=event)
    elif event == "open":
        _check_open(args)
    elif event in PATH_EVENTS:
        _check_path_event(event, args)


# ------------------------------------------------------------------ os.environ -----------------
class _GuardedEnviron:
    """`os.environ` raises instead of answering.

    No audit event fires for reading the environment, so there is nothing to hook — this is the one
    escape that needs an explicit guard. The REAL protection is that the child inherits nothing
    (see `LocalIsolatedRunner`: `env={}`), so even a successful read yields no keys. This object
    exists to make the attempt *named* rather than silently empty, which is the difference between
    "the skill found nothing" and "the skill went looking".
    """

    __slots__ = ()

    def _refuse(self, how: str):
        raise SkillViolation("environment", f"reading os.environ ({how}) is not permitted "
                                            f"— the child inherits no environment")

    def __getitem__(self, k): self._refuse("[]")
    def get(self, *a, **k): self._refuse(".get()")
    def __contains__(self, k): self._refuse("`in`")
    def __iter__(self): self._refuse("iteration")
    def __len__(self): self._refuse("len()")
    def keys(self): self._refuse(".keys()")
    def values(self): self._refuse(".values()")
    def items(self): self._refuse(".items()")
    def copy(self): self._refuse(".copy()")
    def __repr__(self): return "<os.environ: refused in the skill runtime>"


def _install_api(scratch: str) -> None:
    """The one thing a skill is handed: where it may write.

    It cannot be told through `os.environ` (guarded, and empty), and it must not be a magic key in
    the inputs (a skill would then have to declare it in `reads`, putting a runtime detail into the
    manifest the owner approves). So it is a module. `import skillrt_api` is the whole contract:

        import skillrt_api
        open(os.path.join(skillrt_api.scratch_dir, "work.json"), "w")

    A skill that only computes never imports it, which is the common case.
    """
    import types
    api = types.ModuleType("skillrt_api")
    api.scratch_dir = scratch
    api.__doc__ = "The skill runtime's surface. `scratch_dir` is the only writable path."
    sys.modules["skillrt_api"] = api


def _guard_environment() -> None:
    os.environ = _GuardedEnviron()  # type: ignore[assignment]

    def _getenv(*_a, **_k):
        raise SkillViolation("environment", "reading os.getenv() is not permitted "
                                            "— the child inherits no environment")

    os.getenv = _getenv  # type: ignore[assignment]


# ------------------------------------------------------------------ limits ---------------------
def _apply_limits(lim: dict) -> dict:
    """Best-effort OS caps. Returns what was ACTUALLY applied — the report must not claim a cap
    that the platform refused (macOS is strict about `RLIMIT_AS` in ways Linux is not)."""
    applied: dict = {}
    cpu = int(lim.get("cpu_seconds", 5) or 0)
    if cpu > 0:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
            applied["cpu_seconds"] = cpu
        except (ValueError, OSError):
            pass
    mem_mb = int(lim.get("memory_mb", 0) or 0)
    if mem_mb > 0:
        try:
            n = mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (n, n))
            applied["memory_mb"] = mem_mb
        except (ValueError, OSError):
            pass
    fs_mb = int(lim.get("file_size_mb", 0) or 0)
    if fs_mb > 0:
        try:
            n = fs_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (n, n))
            applied["file_size_mb"] = fs_mb
        except (ValueError, OSError):
            pass
    # RLIMIT_NPROC is deliberately NOT set: on Linux it counts threads as well as processes, so a
    # tight value can wedge the child before the skill runs, and fork/exec are already refused at
    # the Python level by DENY_EVENTS. A cap that breaks the runtime is not a security control.
    return applied


# ------------------------------------------------------------------ main -----------------------
def _format_exc(exc: BaseException) -> str:
    """Format a traceback WITHOUT reading any source lines.

    `traceback.format_exc()` resolves each frame through `linecache`, which `open()`s that frame's
    source file — and one of those frames is `_bootstrap.py`, which is deliberately not a readable
    root from inside the guard. The guard then correctly refuses the frame's own file, the
    formatting raises, and the child dies before it can report anything: a skill that raised a
    plain `ValueError` came back as "exited without a result".

    `lookup_lines=False` keeps the frames and the exception and drops the quoted source text. The
    module and line number of each frame are what the report needs; the source line is a
    convenience, and it is not worth the runtime being unable to explain a failure.
    """
    try:
        return "".join(traceback.TracebackException.from_exception(
            exc, lookup_lines=False).format())[-4000:]
    except Exception:                      # never let error reporting become the error
        return f"{type(exc).__name__}: {exc}"


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        b = os.read(fd, 65536)
        if not b:
            return b"".join(chunks)
        chunks.append(b)


def _read_job(spec: str) -> bytes:
    """`fd:N` or `path:P`. The local runner hands over an inherited descriptor — there is then no
    path to allow-list and nothing for a later `open` check to have to special-case. A sandbox that
    cannot pass descriptors (Contree) uses a path, which is read HERE, before the hook is armed."""
    if spec.startswith("fd:"):
        fd = int(spec[3:])
        try:
            return _read_all(fd)
        finally:
            os.close(fd)
    with open(spec[5:] if spec.startswith("path:") else spec, "rb") as fh:
        return fh.read()


def _open_result_sink(spec: str) -> int:
    """Turn the result spec into a DESCRIPTOR, before the hook is armed.

    Both backends end up writing through a descriptor, and that is deliberate. Writing the result
    with `open(path, "wb")` after arming would fire the `open` audit event and the guard would
    refuse it — correctly, because the result file is not inside the scratch directory the skill was
    given. The alternative, adding the result path to the write roots, would hand the skill a
    sanctioned reason to write the very file its verdict is read from. Opening it here, while the
    hook is still disarmed, means the skill is given no path it can open and no descriptor it can
    name; `os.write` on a descriptor the runtime already holds raises no audit event at all.
    """
    if spec.startswith("fd:"):
        return int(spec[3:])
    return os.open(spec[5:] if spec.startswith("path:") else spec,
                   os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)


def _write_result(fd: int, payload: dict) -> None:
    os.write(fd, json.dumps(payload).encode("utf-8"))


def main(argv: list[str]) -> int:
    job_spec, result_spec = argv[1], argv[2]
    job = json.loads(_read_job(job_spec).decode("utf-8"))
    result_fd = _open_result_sink(result_spec)

    # A skill CALL takes the inputs object and must return an object. A held-out TEST
    # function takes nothing and passes by not raising. Declared by the caller rather
    # than introspected, so a skill cannot change how it is invoked from its own def line.
    pass_inputs = bool(job.get("pass_inputs", True))

    limits_applied = _apply_limits(job.get("limits") or {})

    # The skill must not be able to learn the result descriptor from its own argv.
    sys.argv = [job.get("module") or "skill"]

    skill_dir = job["skill_dir"]
    scratch = job["scratch_dir"]
    roots = {
        "read_roots": [skill_dir, scratch] + [str(p) for p in job.get("read_roots") or []],
        "write_roots": [scratch],
    }

    module = None
    try:
        STATE.update(roots)
        # Register FIRST, then arm: the hook body is a no-op until `armed` is set, so nothing the
        # runtime does between these two lines has to be allow-listed.
        sys.addaudithook(_hook)
        STATE["armed"] = True
        _guard_environment()
        _install_api(scratch)
        sys.dont_write_bytecode = True
        sys.path.insert(0, skill_dir)
        # The extra roots are importable only for the duration of the import below. For the harness
        # that root holds the HELD-OUT TESTS, which is the point: the tests must be importable so
        # the skill can be graded by them, and unreadable immediately afterwards so the skill being
        # graded cannot read the tests it is being graded by.
        path_before = list(sys.path)
        for extra in reversed(STATE["read_roots"][2:]):
            sys.path.insert(0, extra)
        __import__(job["module"])
        module = sys.modules[job["module"]]
        STATE["read_roots"] = [skill_dir, scratch]
        sys.path[:] = path_before
    except SkillViolation as exc:
        # A hostile import dies here, with the hook already armed — this is the path the
        # `importing ctypes` / `importing socket` escape tests take.
        _write_result(result_fd, {"status": "violation", "violation": exc.as_dict(),
                                  "result": None, "error": str(exc)})
        return 3
    except BaseException as exc:  # a skill that cannot even be imported
        _write_result(result_fd, {
            "status": "error", "result": None, "violation": None,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": _format_exc(exc)})
        return 2

    entry = getattr(module, job["entry"], None)
    if not callable(entry):
        _write_result(result_fd, {"status": "error", "result": None, "violation": None,
                                  "error": f"module {job['module']!r} has no callable "
                                           f"{job['entry']!r}"})
        return 2

    try:
        # A skill entry point takes the inputs object; a held-out TEST function takes
        # nothing. The distinction is declared by the caller rather than guessed from the
        # signature, so a skill cannot alter how it is invoked by changing its own def line.
        result = entry(dict(job.get("inputs") or {})) if pass_inputs else entry()
    except SkillViolation as exc:
        _write_result(result_fd, {"status": "violation", "violation": exc.as_dict(),
                                  "result": None, "error": str(exc)})
        return 3
    except BaseException as exc:
        _write_result(result_fd, {"status": "error", "result": None, "violation": None,
                                  "error": f"{type(exc).__name__}: {exc}",
                                  "traceback": _format_exc(exc)})
        return 2

    if pass_inputs and not isinstance(result, dict):
        _write_result(result_fd, {"status": "error", "result": None, "violation": None,
                                  "error": f"{job['entry']}() returned {type(result).__name__}, "
                                           f"not a dict"})
        return 2

    _write_result(result_fd, {"status": "ok", "result": result if pass_inputs else None,
                              "violation": None, "error": None,
                              "limits_applied": limits_applied})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
