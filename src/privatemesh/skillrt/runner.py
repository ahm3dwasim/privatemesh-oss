"""Running a generated skill without letting it out.

A `Runner` takes a skill directory and a dict of inputs and returns a `RunResult`. There are two
backends behind one protocol:

  * `LocalIsolatedRunner` — a separate Python process on this machine, `-I -S -B`, a fresh empty
    cwd, an environment that inherits NOTHING, OS resource caps, and `_bootstrap.py`'s audit hook
    armed before the skill is imported. On macOS it is additionally wrapped in `sandbox-exec` with
    a deny-network profile when that binary is present.
  * `ContreeRunner` — the same contract against a Nebius Token Factory Sandbox, for when the
    owner's account has beta access. It talks to `contree-sdk` through a narrow client protocol
    (see `ContreeClient`) so the runner is testable today against a fake.

**What the local runner does and does not guarantee.** It is not a VM and this module does not
pretend otherwise. It stops the escapes named in `violations.KINDS` by intercepting CPython's own
audit events — a strong guard for Python-level escapes, and one the skill cannot disarm, because
CPython offers no way to remove an audit hook once installed. It does not stop a CPython
interpreter bug, a native-code escape that raises no audit event, or a skill that forges the result
bytes on the descriptor it was handed. Its containment is defence in depth (empty env, empty cwd,
`-I -S`, resource caps, a network-deny sandbox profile where available), and every layer that was
actually applied is reported in `RunResult.isolation` rather than assumed.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .violations import Violation

BOOTSTRAP = Path(__file__).resolve().with_name("_bootstrap.py")

OK, VIOLATION, ERROR, TIMEOUT = "ok", "violation", "error", "timeout"

#: Sentinels `_spawn` returns in place of an exit code when the PARENT did the killing.
MEMORY = "memory"

#: How often the parent samples the child's RSS while it runs. A sampled watchdog, not a hard cap:
#: a skill that allocates and frees inside one interval is not caught by it. It exists because
#: macOS refuses the rlimits that would have made this unnecessary — see `Limits`.
RSS_POLL_SECONDS = 0.1


def _rss_kb(pid: int) -> int:
    """The child's resident set, or 0 if it cannot be read (a dead pid, a missing `ps`)."""
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/statm", encoding="ascii") as fh:
                return int(fh.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out.split()[0]) if out else 0
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0

#: Signals that mean "the cap did its job" rather than "the skill was broken". `Popen.returncode`
#: is negative for a signalled child, so these are compared as negative numbers.
_RESOURCE_SIGNAL_NUMBERS = {-int(s) for s in (signal.SIGXCPU, signal.SIGKILL, signal.SIGXFSZ)}


@dataclass(frozen=True)
class Limits:
    """The caps the child is asked to live inside.

    Three of them are enforced two different ways, because the platforms differ and pretending
    otherwise would put a cap in the evidence that is not in force:

      * `cpu_seconds` and `file_size_mb` — `setrlimit` in the child; the kernel sends SIGXCPU /
        SIGXFSZ and the death is reported as a `resources` violation;
      * `memory_mb` — `setrlimit(RLIMIT_AS)` where the platform accepts it (Linux), and otherwise a
        parent-side watchdog that samples the child's RSS and kills it. macOS refuses the memory
        rlimits outright here, which is why the watchdog exists rather than the cap simply being
        claimed. The watchdog samples every `RSS_POLL_SECONDS`, so it is a cap on sustained use, not
        on a spike that allocates and frees inside one interval;
      * `wall_seconds` — the parent kills the child and reports a timeout.

    `RunResult.isolation["limits_applied"]` records what the OS actually accepted.
    """

    cpu_seconds: int = 5
    memory_mb: int = 512
    wall_seconds: float = 10.0
    file_size_mb: int = 8
    max_result_bytes: int = 262_144


@dataclass(frozen=True)
class RunSpec:
    """One skill invocation.

    `read_roots` are extra directories readable ONLY while `module` is being imported, and revoked
    before `entry` is called. That is how the harness grades a skill with held-out tests: the test
    module is importable, and the skill it then invokes cannot read the tests it is being judged by.
    """

    skill_dir: Path
    module: str
    entry: str
    inputs: dict = field(default_factory=dict)
    #: False when `entry` is a held-out test function, which takes no arguments.
    pass_inputs: bool = True
    read_roots: tuple[Path, ...] = ()
    limits: Limits = Limits()


@dataclass(frozen=True)
class RunResult:
    status: str
    result: dict | None = None
    violation: Violation | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    backend: str = ""
    isolation: dict = field(default_factory=dict)
    traceback: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


@runtime_checkable
class Runner(Protocol):
    name: str

    def run(self, spec: RunSpec) -> RunResult: ...


# ---------------------------------------------------------------- shared -----------------------
def _result_from_payload(payload: dict, *, backend: str, isolation: dict, stdout: str,
                         stderr: str, exit_code: int | None, duration_ms: int) -> RunResult:
    raw = payload.get("violation")
    violation = Violation(raw["kind"], raw["detail"], raw.get("event", "")) if raw else None
    if payload.get("limits_applied"):
        # Report the caps the OS ACTUALLY accepted, not the ones that were requested: a memory cap
        # macOS silently refused must not appear in the evidence as if it were in force.
        isolation = {**isolation, "limits_applied": payload["limits_applied"]}
    return RunResult(status=payload.get("status", ERROR), result=payload.get("result"),
                     violation=violation, stdout=stdout, stderr=stderr,
                     error=payload.get("error"), exit_code=exit_code, duration_ms=duration_ms,
                     backend=backend, isolation=isolation,
                     traceback=payload.get("traceback", ""))


def _decode(b: bytes | str | None) -> str:
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    return b.decode("utf-8", "replace")


# ---------------------------------------------------------------- local ------------------------
class LocalIsolatedRunner:
    """A separate Python process on this machine, with the guard armed inside it."""

    name = "local-isolated"

    #: The macOS sandbox profile. `(allow default)` first, because a profile tight enough to break
    #: Python's own startup would be a profile that gets switched off in a hurry. The one thing it
    #: takes away is the network, which is the escape the audit hook cannot backstop at C level.
    SANDBOX_PROFILE = "(version 1)\n(allow default)\n(deny network*)\n"

    def __init__(self, *, python: str | None = None, use_sandbox_exec: bool | None = None,
                 keep_workdir: bool = False, sandbox_root: Path | None = None) -> None:
        self.python = python or sys.executable
        self.keep_workdir = keep_workdir
        self.sandbox_root = sandbox_root
        if use_sandbox_exec is None:
            use_sandbox_exec = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None
        self.use_sandbox_exec = bool(use_sandbox_exec)

    def run(self, spec: RunSpec) -> RunResult:
        started = time.perf_counter()
        root = Path(tempfile.mkdtemp(prefix="skillrt-", dir=self.sandbox_root))
        scratch = root / "scratch"
        control = root / "control"
        scratch.mkdir()
        control.mkdir()

        job_path = control / "job.json"
        job_path.write_text(json.dumps({
            "module": spec.module, "entry": spec.entry, "inputs": spec.inputs,
            "pass_inputs": spec.pass_inputs,
            "skill_dir": str(Path(spec.skill_dir).resolve()), "scratch_dir": str(scratch),
            "read_roots": [str(Path(p).resolve()) for p in spec.read_roots],
            "limits": {"cpu_seconds": spec.limits.cpu_seconds,
                       "memory_mb": spec.limits.memory_mb,
                       "file_size_mb": spec.limits.file_size_mb},
        }), encoding="utf-8")
        result_path = control / "result.json"
        result_path.touch()

        job_fd = os.open(job_path, os.O_RDONLY)
        result_fd = os.open(result_path, os.O_RDWR)
        try:
            argv, isolation = self._command(spec, control, job_fd, result_fd)
            stdout, stderr, code = self._spawn(argv, root, spec, job_fd, result_fd)
        finally:
            os.close(job_fd)
            os.close(result_fd)

        # The child writes a result on EVERY path it controls — ok, violation and error alike — so
        # an empty result file means it never got to write one, i.e. it was killed. Reading the file
        # must happen before the workdir is removed.
        body = result_path.read_bytes()[:spec.limits.max_result_bytes]
        parsed = None
        if body.strip():
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None

        duration_ms = int((time.perf_counter() - started) * 1000)
        if self.keep_workdir:
            isolation["workdir"] = str(root)
        else:
            shutil.rmtree(root, ignore_errors=True)

        if parsed is None:
            if code == TIMEOUT:
                return RunResult(status=TIMEOUT, stdout=stdout, stderr=stderr,
                                 duration_ms=duration_ms, backend=self.name,
                                 isolation=isolation,
                                 error=f"exceeded the {spec.limits.wall_seconds}s wall clock")
            if code == MEMORY:
                violation = Violation(
                    "resources",
                    f"the skill passed the {spec.limits.memory_mb} MB memory cap and was killed")
                return RunResult(status=VIOLATION, violation=violation, stdout=stdout,
                                 stderr=stderr, duration_ms=duration_ms, backend=self.name,
                                 isolation=isolation, error=str(violation))
            # `Popen.returncode` is NEGATIVE for a signalled child, and `_RESOURCE_SIGNAL_NUMBERS`
            # holds negatives, so this compares like for like.
            if isinstance(code, int) and code in _RESOURCE_SIGNAL_NUMBERS:
                violation = Violation(
                    "resources",
                    f"the skill was killed by signal {-code} "
                    f"({signal.Signals(-code).name}) — a CPU or file-size cap fired")
                return RunResult(status=VIOLATION, violation=violation, stdout=stdout,
                                 stderr=stderr, exit_code=code, duration_ms=duration_ms,
                                 backend=self.name, isolation=isolation, error=str(violation))
            return RunResult(status=ERROR, stdout=stdout, stderr=stderr, exit_code=code,
                             duration_ms=duration_ms, backend=self.name, isolation=isolation,
                             error=f"the isolated child exited {code} without writing a result "
                                   f"(it was killed, or the runtime failed to start)")

        return _result_from_payload(parsed, backend=self.name, isolation=isolation, stdout=stdout,
                                    stderr=stderr, exit_code=code, duration_ms=duration_ms)

    # ---- internals -----------------------------------------------------------------------
    def _command(self, spec, control: Path, job_fd: int, result_fd: int):
        cmd = [self.python, "-I", "-S", "-B", str(BOOTSTRAP), f"fd:{job_fd}", f"fd:{result_fd}"]
        isolation = {
            "python_flags": ["-I", "-S", "-B"],
            "env_inherited": False,
            "cwd": "fresh empty directory",
            "bootstrap": str(BOOTSTRAP),
            "audit_hook": True,
            "sandbox_exec": False,
            "scratch_dir": "fresh per run",
        }
        if self.use_sandbox_exec:
            profile = control / "sandbox.sb"
            profile.write_text(self.SANDBOX_PROFILE, encoding="utf-8")
            cmd = ["sandbox-exec", "-f", str(profile)] + cmd
            isolation["sandbox_exec"] = True
            isolation["sandbox_profile"] = "allow default, deny network*"
        else:
            isolation["sandbox_exec_note"] = (
                "not applied: sandbox-exec unavailable on this platform"
                if sys.platform != "darwin" else "not applied: sandbox-exec not found on PATH")
        return cmd, isolation

    def _spawn(self, argv, root: Path, spec: RunSpec, job_fd: int, result_fd: int):
        """Returns (stdout, stderr, exit_code). `exit_code` is the TIMEOUT or MEMORY sentinel when
        the parent did the killing rather than the child exiting."""
        proc = subprocess.Popen(
            argv, cwd=str(root), env={},              # nothing inherited, and no PYTHON* vars
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(job_fd, result_fd), close_fds=True, text=False)
        deadline = time.monotonic() + spec.limits.wall_seconds
        cap_kb = spec.limits.memory_mb * 1024
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                out, err = proc.communicate()
                return _decode(out), _decode(err), TIMEOUT
            try:
                out, err = proc.communicate(timeout=min(RSS_POLL_SECONDS, remaining))
                return _decode(out), _decode(err), proc.returncode
            except subprocess.TimeoutExpired:
                # Re-entering communicate() after a timeout does not lose output (documented), so
                # this loop is how the wall clock and the memory watchdog share one wait.
                if cap_kb and _rss_kb(proc.pid) > cap_kb:
                    proc.kill()
                    out, err = proc.communicate()
                    return _decode(out), _decode(err), MEMORY


# ---------------------------------------------------------------- contree -----------------------
class ContreeExec(Protocol):
    exit_code: int
    stdout: str
    stderr: str


class ContreeSandbox(Protocol):
    def upload(self, files: dict[str, bytes]) -> None: ...
    def exec(self, argv: list[str], *, env: dict, timeout_s: int) -> ContreeExec: ...
    def destroy(self) -> None: ...


class ContreeClient(Protocol):
    """The seam onto `contree-sdk`.

    Written as a protocol rather than imported directly on purpose. The SDK is not installed in
    this repo and the owner's Token Factory key currently returns ForbiddenError (sandboxes are
    behind beta access), so a direct import would be a guess about an API nobody here has run.
    An adapter that has actually been exercised against the live service is what turns this from a
    design into a backend; until then the fake in the tests is the only thing that has executed it.
    """

    base_url: str

    def create_sandbox(self, *, image: str, timeout_s: int) -> ContreeSandbox: ...


class ContreeRunner:
    """The same run contract, executed inside a Token Factory Sandbox.

    The value over the local runner is that the isolation stops being Python-level: the skill runs
    on a machine that is not this one, with no route to the owner's files or credentials at all.
    The reason it is not the default is the owner's account: sandboxes are beta-gated, and a run
    that cannot be executed is worse than one that runs locally with its limits reported.
    """

    name = "contree"

    def __init__(self, client: ContreeClient, *, image: str = "python:3.12-slim",
                 base_url: str = "https://api.tokenfactory.nebius.com/sandboxes/") -> None:
        self.client = client
        self.image = image
        self.base_url = base_url

    def run(self, spec: RunSpec) -> RunResult:
        started = time.perf_counter()
        job = {
            "module": spec.module, "entry": spec.entry, "inputs": spec.inputs,
            "pass_inputs": spec.pass_inputs,
            "skill_dir": "/skill", "scratch_dir": "/scratch",
            "read_roots": [f"/heldout/{i}" for i, _ in enumerate(spec.read_roots)],
            "limits": {"cpu_seconds": spec.limits.cpu_seconds,
                       "memory_mb": spec.limits.memory_mb,
                       "file_size_mb": spec.limits.file_size_mb},
        }
        files: dict[str, bytes] = {
            "/skillrt/_bootstrap.py": BOOTSTRAP.read_bytes(),
            "/job.json": json.dumps(job).encode("utf-8"),
        }
        for path in sorted(Path(spec.skill_dir).rglob("*.py")):
            files[f"/skill/{path.relative_to(spec.skill_dir)}"] = path.read_bytes()
        for i, root in enumerate(spec.read_roots):
            for path in sorted(Path(root).rglob("*.py")):
                files[f"/heldout/{i}/{path.relative_to(root)}"] = path.read_bytes()

        sandbox = self.client.create_sandbox(image=self.image,
                                             timeout_s=int(spec.limits.wall_seconds))
        try:
            sandbox.upload(files)
            proc = sandbox.exec(
                ["python", "-I", "-S", "-B", "/skillrt/_bootstrap.py",
                 "path:/job.json", "path:/result.json"],
                env={}, timeout_s=int(spec.limits.wall_seconds) + 5)
            # Read the result BEFORE destroying the sandbox — the file lives in it.
            reader = sandbox.exec(["cat", "/result.json"], env={}, timeout_s=10)
        finally:
            sandbox.destroy()

        duration_ms = int((time.perf_counter() - started) * 1000)
        isolation = {"backend": self.name, "base_url": self.base_url, "image": self.image,
                     "env_inherited": False, "audit_hook": True,
                     "vm_boundary": "the sandbox is a separate machine; no route to local files",
                     "uploads": sorted(files)}

        try:
            payload = json.loads(_decode(reader.stdout))
        except json.JSONDecodeError as exc:
            return RunResult(status=ERROR, stdout=_decode(proc.stdout), stderr=_decode(proc.stderr),
                             exit_code=proc.exit_code, duration_ms=duration_ms,
                             backend=self.name, isolation=isolation,
                             error=f"no result from the sandbox: {exc}")
        return _result_from_payload(payload, backend=self.name, isolation=isolation,
                                    stdout=_decode(proc.stdout), stderr=_decode(proc.stderr),
                                    exit_code=proc.exit_code, duration_ms=duration_ms)
