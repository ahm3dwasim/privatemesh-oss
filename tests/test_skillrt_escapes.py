"""The headline: a hostile skill tries each escape, is STOPPED, and the violation is named.

Every case here has two halves, and the second half is the one that makes the first mean anything:

  * `test_escape_is_stopped_and_named` — the skill runs through `LocalIsolatedRunner` and the run
    ends in a `violation` of the expected kind;
  * `test_the_escape_is_real_when_unguarded` — the SAME source, run with no audit hook at all, gets
    further: the file lands on disk, the canary comes back, the process is replaced. That is the
    mutant these tests are built to kill ("just don't install the hook"). Without it, a test
    asserting "violation" would pass just as happily against a skill that was broken for an
    unrelated reason.

Which escapes can be driven end to end is bounded by what the child already has in memory. `os` and
the builtins are always importable, so `os.system`, `os.fork`, `os.execv`, `open()` and
`os.environ` are reachable and tested that way. `socket`, `subprocess`, `ctypes` and `urllib` are
refused at `import`, before any of their functions can be called — so their *function*-level events
are tested directly against the hook, and the import refusal is tested end to end. The second-to-last
test asserts those function-level events are still wired, so trimming `DENY_EVENTS` down to what the
end-to-end suite happens to exercise fails here.

No test makes a network call.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from privatemesh.skillrt.runner import Limits, LocalIsolatedRunner, RunSpec
from privatemesh.skillrt.violations import KINDS

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "src" / "privatemesh" / "skillrt" / "_bootstrap.py"
WALL = 60.0

#: name -> (skill source, the violation kind the guard must name)
ESCAPES: dict[str, tuple[str, str]] = {
    "import_socket": ("import socket\ndef run(inputs):\n    return {'v': str(socket)}\n", "import"),
    "import_subprocess": ("import subprocess\ndef run(inputs):\n    return {'v': 'x'}\n", "import"),
    "import_ctypes": ("import ctypes\ndef run(inputs):\n    return {'v': 'x'}\n", "import"),
    "import_urllib": ("import urllib.request\ndef run(inputs):\n    return {'v': 'x'}\n", "import"),
    "os_system": ("import os\ndef run(inputs):\n    os.system('touch ' + inputs['marker'])\n"
                  "    return {'v': 'ran'}\n", "subprocess"),
    "os_fork": ("import os\ndef run(inputs):\n    os.fork()\n    return {'v': 'ran'}\n",
                "process_control"),
    "os_execv": ("import os\ndef run(inputs):\n"
                 "    os.execv('/bin/echo', ['echo', 'EXEC_REPLACED_ME'])\n"
                 "    return {'v': 'ran'}\n", "process_control"),
    "read_private_dir": ("def run(inputs):\n    return {'v': open(inputs['secret']).read()}\n",
                         "filesystem"),
    "write_outside_scratch": ("def run(inputs):\n    open(inputs['marker'], 'w').write('pwned')\n"
                              "    return {'v': 'wrote'}\n", "filesystem"),
    "read_environ": ("import os\ndef run(inputs):\n    return {'v': os.environ['PM_CANARY']}\n",
                     "environment"),
}

CANARY = "sk-live-canary-not-a-real-key"


def write_skill(root: Path, name: str, source: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return root


def unguarded_run(skill_dir: Path, module: str, inputs: dict, scratch: Path) -> dict:
    """Run the skill with NO audit hook: the mutant.

    `-I -S -B` is kept so the comparison against the guarded run is like for like — the only
    difference is the guard. The canary is set in the environment because proving the environment
    escape is real means there has to be something in there to find.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    observer = scratch / "_observer.py"
    observer.write_text(textwrap.dedent(f"""
        import json, os, sys
        sys.modules['skillrt_api'] = type(sys)('skillrt_api')
        sys.modules['skillrt_api'].scratch_dir = {str(scratch)!r}
        events = []
        sys.addaudithook(lambda e, a: events.append(e))
        sys.path.insert(0, {str(skill_dir)!r})
        os.chdir({str(scratch)!r})
        import importlib
        error = result = None
        try:
            result = importlib.import_module({module!r}).run({inputs!r})
        except BaseException as exc:
            error = f"{{type(exc).__name__}}: {{exc}}"
        sys.stderr.write("@@" + json.dumps({{"events": events, "error": error,
                                             "result": result}}) + "@@")
        sys.stderr.flush()
    """), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", "-S", "-B", str(observer)],
                          capture_output=True, text=True, timeout=WALL,
                          env={"PM_CANARY": CANARY})
    payload = (proc.stderr.split("@@")[1] if "@@" in proc.stderr
               else '{"events": [], "error": "the observer never reported", "result": null}')
    observed = json.loads(payload)
    observed["stdout"] = proc.stdout
    return observed


#: name -> what the UNGUARDED run must show. Each one is the direct evidence that the guarded test
#: above is measuring the guard and not a coincidence.
def _mutant_holds(name: str, observed: dict) -> bool:
    if name == "write_outside_scratch":
        return observed["result"] == {"v": "wrote"}
    if name == "os_system":
        return "os.system" in observed["events"]
    if name == "os_fork":
        return "os.fork" in observed["events"]
    if name == "os_execv":
        return "EXEC_REPLACED_ME" in observed["stdout"]
    if name == "read_private_dir":
        return observed["result"] == {"v": "private"}
    if name == "read_environ":
        return observed["result"] == {"v": CANARY}
    if name.startswith("import_"):
        return observed["error"] is None and observed["result"] is not None
    raise AssertionError(f"no mutant probe defined for {name}")


@pytest.fixture
def runner():
    return LocalIsolatedRunner()


def _inputs(tmp_path: Path) -> dict:
    marker = tmp_path / "ESCAPED"
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    return {"marker": str(marker), "secret": str(secret)}


@pytest.mark.parametrize("name", sorted(ESCAPES))
def test_escape_is_stopped_and_named(name, runner, tmp_path):
    """The guard stops it, and the report can say WHICH escape it was — not merely that it failed."""
    source, kind = ESCAPES[name]
    skill = write_skill(tmp_path / "skill", name, source)
    inputs = _inputs(tmp_path)

    result = runner.run(RunSpec(skill_dir=skill, module=name, entry="run", inputs=inputs,
                                limits=Limits(wall_seconds=WALL)))

    assert result.status == "violation", f"{name} was not stopped: {result.status} {result.error}"
    assert result.violation is not None and result.violation.kind == kind, \
        f"{name} raised the wrong kind: {result.violation}"
    assert kind in KINDS
    assert not (tmp_path / "ESCAPED").exists(), f"{name} ESCAPED: the marker was created"


@pytest.mark.parametrize("name", sorted(ESCAPES))
def test_the_escape_is_real_when_unguarded(name, tmp_path):
    """The mutant: remove the guard and the same code gets further.

    This is what makes the test above evidence rather than decoration.
    """
    source, _ = ESCAPES[name]
    skill = write_skill(tmp_path / "skill", name, source)
    observed = unguarded_run(skill, name, _inputs(tmp_path), tmp_path / "scratch")

    assert _mutant_holds(name, observed), \
        f"unguarded, {name} did not escape — so the guarded test above proves nothing: {observed}"


def test_a_skill_cannot_read_the_held_out_tests_that_grade_it(runner, tmp_path):
    """The revocation, end to end.

    The held-out directory is readable while the test module is imported (it has to be) and revoked
    the instant that import finishes. A skill that then goes looking for the tests grading it — which
    is exactly what a skill trying to pass would do — is stopped, and the violation names the path.
    """
    skill = write_skill(tmp_path / "skill", "peeker", """
        def run(inputs):
            with open(inputs['probe']) as fh:
                return {'v': fh.read()}
    """)
    held_out = tmp_path / "held_out"
    held_out.mkdir()
    probe = held_out / "expectations.txt"
    probe.write_text("held-out expectations", encoding="utf-8")

    result = runner.run(RunSpec(skill_dir=skill, module="peeker", entry="run",
                                inputs={"probe": str(probe)}, read_roots=(held_out,),
                                limits=Limits(wall_seconds=WALL)))
    assert result.status == "violation" and result.violation.kind == "filesystem"
    assert "held_out" in result.violation.detail

    # ...and unguarded the same skill reads it, so the refusal is the revocation and not a typo.
    observed = unguarded_run(skill, "peeker", {"probe": str(probe)}, tmp_path / "scratch")
    assert observed["result"] == {"v": "held-out expectations"}


def test_a_skill_can_still_write_inside_its_scratch_dir(runner, tmp_path):
    """The positive control.

    Without this, every escape test above would pass just as well against a runtime that refuses all
    file access — which is a different and useless guarantee. A skill has to be able to compute and
    to use its scratch space, or there is nothing here worth isolating.
    """
    skill = write_skill(tmp_path / "skill", "worker", """
        import json, os, skillrt_api
        def run(inputs):
            path = os.path.join(skillrt_api.scratch_dir, 'work.json')
            with open(path, 'w') as fh:
                json.dump({'slots': inputs['slots']}, fh)
            with open(path) as fh:
                return {'v': json.load(fh)['slots'][0]}
    """)
    result = runner.run(RunSpec(skill_dir=skill, module="worker", entry="run",
                                inputs={"slots": ["15:00", "16:00"]},
                                limits=Limits(wall_seconds=WALL)))
    assert result.status == "ok", result.error
    assert result.result == {"v": "15:00"}


def test_function_level_events_are_still_wired():
    """`socket.connect`, `subprocess.Popen` and the rest cannot be reached end to end — their modules
    are refused at `import` — so they are checked against the hook itself.

    This fails if someone trims `DENY_EVENTS` to the events the end-to-end suite happens to exercise,
    which would silently remove the defence-in-depth layer for a socket obtained some other way.
    """
    boot = _load_bootstrap()
    boot.STATE["armed"] = True          # the hook is a no-op until the bootstrap arms it
    for event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
                  "socket.sendto", "subprocess.Popen", "ctypes.dlopen", "ctypes.dlsym",
                  "urllib.Request", "http.client.connect", "smtplib.connect", "os.posix_spawn",
                  "os.spawn", "os.putenv"):
        with pytest.raises(boot.SkillViolation):
            boot._hook(event, ("example.com", 443))


def test_the_child_and_parent_agree_on_the_violation_vocabulary():
    """The bootstrap cannot import the package (`-I -S`), so it re-declares the vocabulary it may
    name. The child's list is a SUBSET of the parent's — the parent knows two kinds the child cannot
    raise (`proposal`, `manifest`, which belong to the broker boundary).

    Two things are checked, and the second is the one with teeth: every kind the child's own guards
    can actually raise is in its declared list, so a `DENY_EVENTS` entry naming a kind the child
    never declared fails here rather than at three in the morning.
    """
    tree = ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))
    child_kinds = None
    deny_events = None
    for node in tree.body:
        name = getattr(node.targets[0], "id", "") if isinstance(node, ast.Assign) else ""
        if name == "KINDS":
            child_kinds = tuple(ast.literal_eval(node.value))
        elif name == "DENY_EVENTS":
            deny_events = ast.literal_eval(node.value)

    assert child_kinds, "the bootstrap no longer declares KINDS"
    assert set(child_kinds) <= set(KINDS), \
        f"the child names kinds the parent does not know: {set(child_kinds) - set(KINDS)}"
    assert deny_events, "the bootstrap no longer declares DENY_EVENTS"
    referenced = {kind for kind, _ in deny_events.values()}
    assert referenced <= set(child_kinds), \
        f"DENY_EVENTS names kinds missing from KINDS: {referenced - set(child_kinds)}"


def test_env_is_not_inherited_at_all(runner, tmp_path, monkeypatch):
    """An escape test that only proves `os.environ` raises would still pass if the child had
    inherited the parent's environment and the proxy were the only thing standing. This asserts the
    belt as well as the braces: the child is started with `env={}`."""
    monkeypatch.setenv("PM_CANARY", CANARY)
    skill = write_skill(tmp_path / "skill", "envdump", """
        import os
        def run(inputs):
            return {'v': len(list(os.environ.keys()))}
    """)
    result = runner.run(RunSpec(skill_dir=skill, module="envdump", entry="run",
                                limits=Limits(wall_seconds=WALL)))
    assert result.status == "violation"
    assert result.violation.kind == "environment"
    assert result.isolation["env_inherited"] is False


def _load_bootstrap():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_skillrt_bootstrap", BOOTSTRAP)
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    return boot
