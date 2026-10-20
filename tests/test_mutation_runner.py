"""The mutation runner's VERDICT — the review's New-7, and the reason it is a test file.

"All eight controls are caught" was not reproducible in round 1 and was not true in round 2, for
the same reason both times: the runner asked `proc.returncode != 0`. That is satisfied by a
misspelled node id (pytest exits 4), by a module that will not import, by a fixture that blows up
on an unwritable temp directory, and by a collection error — none of which has anything to do with
whether a guard is protected. A harness whose job is to refuse false confidence was manufacturing
it, and the number it printed could not be trusted in either direction.

So the decision is a function of its own here (`verdict`), and it is tested against the output
shapes it has to tell apart. The cases below are real pytest output, captured from this repo's
venv, not paraphrases.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "mutcheck_findings", ROOT / "redteam" / "mutcheck_findings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verdict():
    return _module().verdict


#: The three tiers, taken from the runner rather than restated: a test that hard-coded the strings
#: would keep passing if the runner renamed a tier and `main` went on comparing the old name.
COVERED, UNASSERTED, UNCOVERED = (_module().COVERED, _module().UNASSERTED, _module().UNCOVERED)


#: Captured from `pytest tests/test_agreements.py::test_x -q --no-header -x`.
ASSERTION_FAILED = """\
F
=================================== FAILURES ===================================
__________________________________ test_x __________________________________

    def test_x():
>       assert 1 == 2
E       assert 1 == 2

tests/test_agreements.py:4: AssertionError
=========================== short test summary info ============================
FAILED tests/test_agreements.py::test_x - assert 1 == 2
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.01s
"""


def test_a_real_assertion_failure_is_the_only_thing_that_counts_as_caught():
    tier, why = _verdict()(1, ASSERTION_FAILED, "tests/test_agreements.py::test_x")
    assert tier == COVERED, why


def test_a_misspelled_test_node_is_not_a_caught_mutant():
    """The review's example, and the one that produced a false "all caught" in two rounds."""
    output = ("ERROR: not found: tests/test_agreements.py::test_typoed_name\n"
              "no tests ran in 0.00s\n")
    tier, why = _verdict()(4, output, "tests/test_agreements.py::test_typoed_name")
    assert tier == UNCOVERED and "did not get as far as failing" in why


def test_a_collection_error_is_not_a_caught_mutant():
    output = ("E   ModuleNotFoundError: No module named 'privatemesh.agreements'\n"
              "=========================== short test summary info ============================\n"
              "ERROR tests/test_agreements.py\n"
              "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
              "1 error in 0.24s\n")
    tier, why = _verdict()(2, output, "tests/test_agreements.py::test_x")
    assert tier == UNCOVERED and "did not get as far" in why


def test_a_fixture_error_is_not_a_caught_mutant():
    """Exit 1 with an ERROR: the run got as far as failing, but no test decided anything."""
    output = ("ERROR tests/test_agreements.py::test_x - PermissionError: [Errno 13] Permission denied\n"
              "=========================== short test summary info ============================\n"
              "ERROR tests/test_agreements.py::test_x\n"
              "1 error in 0.05s\n")
    tier, why = _verdict()(1, output, "tests/test_agreements.py::test_x")
    assert tier == UNCOVERED and "error" in why


def test_a_different_test_failing_does_not_cover_this_guard():
    """The named test stayed green and its neighbour went red. The guard is not covered."""
    output = ASSERTION_FAILED.replace("test_x", "some_other_test")
    tier, why = _verdict()(1, output, "tests/test_agreements.py::test_x")
    assert tier == UNCOVERED and "not tests/test_agreements.py::test_x" in why


def test_a_test_that_dies_on_an_exception_is_reported_as_its_own_tier():
    output = ("F\n"
              "=================================== FAILURES ===================================\n"
              "__________________________________ test_x __________________________________\n"
              "    def test_x():\n"
              ">       store.record(**kwargs)\n"
              "E       TypeError: record() got an unexpected keyword argument 'sequence'\n"
              "\n"
              "tests/test_agreements.py:9: TypeError\n"
              "=========================== short test summary info ============================\n"
              "FAILED tests/test_agreements.py::test_x - TypeError: record() got an unexpected "
              "keyword argument 'sequence'\n"
              "1 failed in 0.02s\n")
    tier, why = _verdict()(1, output, "tests/test_agreements.py::test_x")
    # Not COVERED — the brief counts only assertions — and not UNCOVERED either: the guard
    # IS reached, so calling it "not protected" would be a false negative that hides a
    # real defect of the test (it should state the abort as an assertion).
    assert tier == UNASSERTED and "not on an assertion" in why


def test_a_parametrised_control_is_matched_by_its_name_not_its_case():
    """"Did THIS test fail" is the question, and a case id is not part of the answer."""
    output = ASSERTION_FAILED.replace(
        "FAILED tests/test_agreements.py::test_x -",
        "FAILED tests/test_agreements.py::test_x[status2] -")
    tier, why = _verdict()(1, output, "tests/test_agreements.py::test_x")
    assert tier == COVERED, why


def test_a_green_run_is_reported_as_uncovered_rather_than_as_a_pass():
    tier, why = _verdict()(0, "46 passed in 0.40s\n", "tests/test_agreements.py::test_x")
    assert tier == UNCOVERED and "passed with the guard disabled" in why


def test_a_killed_run_puts_the_mutated_file_back(tmp_path):
    """A `finally` does not run when the process is killed, and this runs under `timeout`.

    The mutant that survived the first run of this file was RT-4's, in `src/privatemesh/redteam.py`,
    and the next run read it as the original, found the guard text gone and reported the control as
    a SKIP. The handler is the fix; this holds it to restoring the bytes and stopping.

    In-process, and so the weakest of the three tests of this: it calls the handler directly and
    proves only that the handler does the right thing, not that a real SIGTERM reaches it. The
    subprocess tests further down are the ones that hold the WIRING to it.
    """
    module = _module()
    target = tmp_path / "src.py"
    pristine = b"guard = True\n"
    target.write_bytes(pristine)
    mode = target.stat().st_mode
    module._in_flight = module._InFlight(target, pristine, mode)
    target.write_bytes(b"guard = False\n")                   # the mutation is in the tree

    try:
        module._restore_in_flight()
    except SystemExit:
        pass
    else:
        raise AssertionError("the handler let the run continue with a mutated tree")

    assert target.read_bytes() == pristine, "the mutated source was left behind"


# ------------------------------------------------------------------- the anchor -----------------
# The runner's fourth outcome, and the one the count had been wrong about. `verdict` decides what a
# run is worth; these decide whether there was a run worth deciding about. A control whose guard
# text is gone was reported as SKIP, which reads as "not applicable today" and meant "this guard has
# not been tested since commit 00adac8 reworded the call it names" — and 136 was still printed.


def _plant(module, monkeypatch, tmp_path, source: str) -> None:
    """Aim the runner at a scratch tree holding `source`, with one control list of our choosing."""
    (tmp_path / "src.py").write_text(source)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["mutcheck_findings.py"])   # no --all, so MUTATIONS is read


def _one_control(guard: str) -> list:
    return [("PLANTED-1 a control with no site to mutate", "src.py", guard,
             "guard = False\n", "tests/test_planted.py::test_planted")]


def test_a_control_whose_guard_text_is_missing_fails_the_run(tmp_path, monkeypatch, capsys):
    module = _module()
    _plant(module, monkeypatch, tmp_path, "guard = True\n")
    monkeypatch.setattr(module, "MUTATIONS", _one_control("guard = False\n"))

    code = module.main()

    printed = capsys.readouterr().out
    assert code != 0, "a control that could not be armed was reported as a pass"
    assert "PLANTED-1" in printed, "the failing control was not named"
    assert "SKIP" not in printed, "an unarmed control is still being reported as a skip"
    assert (tmp_path / "src.py").read_text() == "guard = True\n", "an unarmed control mutated a file"


def test_a_control_whose_guard_text_matches_two_sites_fails_the_run(tmp_path, monkeypatch, capsys):
    """The sibling defect, and why both are checked rather than only the reported one.

    `replace(..., 1)` mutates the FIRST occurrence. Five shipped controls named two sites each; all
    five happened to be ordered so that the first was the site their test covers, so their verdicts
    were right — by luck, and by a luck that no reader could audit from the output.
    """
    module = _module()
    _plant(module, monkeypatch, tmp_path, "guard = True\nguard = True\n")
    monkeypatch.setattr(module, "MUTATIONS", _one_control("guard = True\n"))

    code = module.main()

    printed = capsys.readouterr().out
    assert code != 0, "an ambiguous anchor was reported as a pass"
    assert "PLANTED-1" in printed and "2 times" in printed, printed
    assert (tmp_path / "src.py").read_text() == "guard = True\nguard = True\n"


def test_every_shipped_control_anchors_on_exactly_one_site():
    """The regression that matters, run over the real lists rather than a planted one.

    A control may name neither zero sites nor two. Zero is the LIMITS-1 defect; two is the same
    defect one turn further on, where the verdict is about a line the named test never reaches.
    Neither is visible in `covered/len(controls)`, which is why this is a test and not a comment.
    """
    module = _module()
    lists = sorted(name for name in dir(module) if name.startswith("MUTATIONS"))
    assert lists, "no control lists were found — this test would pass vacuously"
    offenders = []
    for name in lists:
        for label, rel, guard, _mutant, _test in getattr(module, name):
            found = (ROOT / rel).read_text().count(guard)
            if found != 1:
                offenders.append(f"{name}: {label} anchors on {found} site(s) in {rel}")
    assert not offenders, "controls that cannot be armed:\n  " + "\n  ".join(offenders)


# ------------------------------------------------------- the exit paths that are not a `finally` --
# The loop's `finally` covers an exception and a normal return. It does not cover the process being
# KILLED, which is how this runner actually ends: under `timeout`, or at a Ctrl-C, or at a `kill -9`
# from an impatient reader. Those are the runs that damaged the tree, so these tests drive the
# runner as a REAL subprocess and signal it — the only way to hold a signal handler, an `atexit`,
# and a journal that outlives the process to what they claim.
#
# Each asserts on BYTES. "The file still parses" is not the promise; "the file is the file it was"
# is, and the difference is exactly the defect these were written for.

RUNNER = ROOT / "redteam" / "mutcheck_findings.py"
JOURNAL = ".mutcheck-inflight.json"

#: The planted fixture, with CRLF line endings on purpose.
#:
#: `read_text()`/`write_text()` decode universal newlines, so a restore routed through them writes
#: LF back where CRLF was — text-identical and not byte-identical. A tree checked out with
#: `core.autocrlf=true` is the real-world version of this, and it is the reason every assertion
#: below is on bytes. It is also what makes the SIGTERM and SIGINT tests red against a runner whose
#: only restore is the text one: they are not vacuous "the handler ran" checks.
PRISTINE = b"# planted fixture\r\nguard = True\r\n"
MUTANT = b"# planted fixture\r\nguard = False\r\n"


def _plant_test(tmp_path, sleep: float) -> None:
    """Write the planted control's test. `sleep` keeps the mutation window open long enough to hit."""
    (tmp_path / "tests" / "test_planted.py").write_text(
        "import time\n\n\ndef test_planted():\n"
        f"    time.sleep({sleep!r})\n"
        "    assert False, 'the planted guard is gone'\n", encoding="utf-8")


def _plant_tree(tmp_path, sleep: float, driver_extra: str = ""):
    """A throwaway copy of the runner over a planted fixture, with one control.

    The runner is COPIED rather than pointed at the real one. `ROOT` is derived from `__file__`, so
    the copy in `tmp_path/redteam/` gets its own scratch tree for free: `src.py` here is the only
    file any of this can mutate, and no real module is ever a candidate. Nothing is stubbed — the
    process under test is this repo's own runner, run the way the README runs it.

    `driver_extra` is inserted between the control list and `main()`, which is where a test that
    needs to reach INSIDE the runner puts its patch: through the module the driver has just
    imported, on a name the runner looks up at call time.
    """
    (tmp_path / "redteam").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(exist_ok=True)
    shutil.copy(RUNNER, tmp_path / "redteam" / "mutcheck_findings.py")
    _plant_test(tmp_path, sleep)
    source = tmp_path / "src.py"
    source.write_bytes(PRISTINE)
    (tmp_path / "driver.py").write_text(
        "import importlib.util, pathlib, sys\n"
        "here = pathlib.Path(__file__).resolve().parent\n"
        "spec = importlib.util.spec_from_file_location(\n"
        "    'mutcheck_findings', here / 'redteam' / 'mutcheck_findings.py')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "module.MUTATIONS = [('PLANTED-1', 'src.py', 'guard = True', 'guard = False',\n"
        "                     'tests/test_planted.py::test_planted')]\n"
        + driver_extra +
        "sys.exit(module.main())\n", encoding="utf-8")
    return source, tmp_path / "driver.py"


def _start(tmp_path):
    return subprocess.Popen([sys.executable, str(tmp_path / "driver.py")], cwd=tmp_path,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_for_mutation(proc, source) -> None:
    """Return once the planted file is mutated — i.e. once the runner is inside the window."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if source.read_bytes() != PRISTINE:
            return
        if proc.poll() is not None:
            raise AssertionError("the runner exited without mutating anything — nothing was tested")
        time.sleep(0.005)
    proc.kill()
    raise AssertionError("the planted file was never mutated — nothing was tested")


def _interrupted_by(tmp_path, sig, sleep: float = 3):
    """Start a run, wait until the mutant is on disk, deliver `sig`, and return the pieces.

    The mode is read BEFORE the run rather than assumed: it is the thing "put the file back" has to
    include, and a hard-coded 0o644 would agree with a fresh temp file whatever the runner did.
    """
    source, _ = _plant_tree(tmp_path, sleep)
    mode = stat.S_IMODE(source.stat().st_mode)
    proc = _start(tmp_path)
    _wait_for_mutation(proc, source)
    proc.send_signal(sig)
    return proc, source, mode


def _assert_untouched(source, proc, sig, mode, tmp_path):
    out, err = proc.communicate(timeout=30)
    after = source.read_bytes()
    assert after == PRISTINE, (
        f"{sig!r} left the planted file changed.\n  before {PRISTINE!r}\n  after  {after!r}\n"
        f"stdout={out}\nstderr={err}")
    assert stat.S_IMODE(source.stat().st_mode) == mode, "the file came back with a different mode"
    assert not (tmp_path / JOURNAL).exists(), (
        "the run left its journal behind, so the next run would 'recover' a state that is already "
        "correct — a run that exits cleanly must leave nothing to recover")


def test_sigterm_mid_mutation_leaves_the_file_byte_identical(tmp_path):
    """`timeout 3000` is SIGTERM, and it is how this runner is normally given a deadline."""
    proc, source, mode = _interrupted_by(tmp_path, signal.SIGTERM)
    _assert_untouched(source, proc, signal.SIGTERM, mode, tmp_path)


def test_sigint_mid_mutation_leaves_the_file_byte_identical(tmp_path):
    """The judge who reads the output, loses patience, and hits Ctrl-C."""
    proc, source, mode = _interrupted_by(tmp_path, signal.SIGINT)
    _assert_untouched(source, proc, signal.SIGINT, mode, tmp_path)


#: A driver patch that holds the restore open between the START of `_restore_now` and its write.
#:
#: The window the review's P1 names is a few bytecodes wide, so a test cannot reach it by timing —
#: it has to be held open from inside. Pausing at the WRITE is what makes this seam work against
#: both the old runner and the new one: `_restore_now` calls `_write_exact` in each, so a signal
#: delivered while this is waiting is a signal delivered between the start of the restore and the
#: bytes going back. The first call announces itself and waits; later calls go straight through, so
#: a runner that legitimately restores twice is not blocked by its own test.
_PAUSE_INSIDE_THE_RESTORE = (
    "import time\n"
    "real_write_exact = module._write_exact\n"
    "paused = [False]\n"
    "def _write_exact_that_pauses(path, data, mode):\n"
    "    if not paused[0]:\n"
    "        paused[0] = True\n"
    "        (here / 'restore-paused').write_text('paused')\n"
    "        deadline = time.monotonic() + 60\n"
    "        while not (here / 'restore-go').exists() and time.monotonic() < deadline:\n"
    "            time.sleep(0.005)\n"
    "    return real_write_exact(path, data, mode)\n"
    "module._write_exact = _write_exact_that_pauses\n"
)

#: Takes the `atexit` backstop away for this test, so the signal path is the only thing that can
#: put the file back. Without it the assertion below would hold even if the handlers did nothing at
#: all — `atexit` restores on the way out — and the test would be measuring the wrong defence.
#: `main()` reaches `atexit.register` as a global lookup, so replacing the module's `atexit` is
#: enough, and the runner's own code is left alone.
_NO_ATEXIT_BACKSTOP = (
    "class _NoBackstop:\n"
    "    def register(self, *args, **kwargs):\n"
    "        pass\n"
    "module.atexit = _NoBackstop()\n"
)


def _wait_for_the_restore_to_open(marker: Path) -> None:
    """Return once the runner has announced it is inside the restore. No timing assumption.

    The runner says so itself, from inside the window. Waiting for that rather than sleeping and
    hoping is the difference between a test of the defect and a test of the machine's load: on a
    fast run a fixed sleep would deliver the second signal after the restore had finished, and the
    test would pass while proving nothing.
    """
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if marker.exists():
            return
        time.sleep(0.005)
    raise AssertionError("the runner never entered the restore window — nothing was tested")


def test_a_second_signal_inside_the_restore_still_puts_the_file_back(tmp_path):
    """The review's P1, and the shape a single-signal test cannot see.

    `_restore_now` dropped `_in_flight` — its only in-memory record of what the tree is missing —
    BEFORE it wrote anything back. A second signal landing in that gap therefore made both the
    handler and the `atexit` no-ops: the second handler found nothing to do, raised `SystemExit`
    through the first handler's frame, and aborted the write the first one was in the middle of.
    `restore` became `interrupted restore`, which is the one outcome this whole file exists to
    prevent, and a second SIGTERM is not exotic — `timeout` sends one at the deadline, an impatient
    `kill` sends another, and a shell closing sends SIGHUP on top.

    What the runner must do instead is keep the record — in memory AND in the journal — until the
    bytes are back and re-read, and let a second call redo the restore rather than short-circuit it.
    Then the second signal is harmless: whichever frame survives, the file has been put back.

    Deterministic by construction. The runner is held open inside the window and says so; the signal
    is delivered only after that, so it cannot miss the interval no matter how fast the machine is.
    The `atexit` backstop is removed for the duration, so what the assertions below see is the
    signal path's own work and not the exit path cleaning up behind it.
    """
    source, _ = _plant_tree(tmp_path, sleep=3,
                            driver_extra=_PAUSE_INSIDE_THE_RESTORE + _NO_ATEXIT_BACKSTOP)
    mode = stat.S_IMODE(source.stat().st_mode)
    proc = _start(tmp_path)
    _wait_for_mutation(proc, source)

    proc.send_signal(signal.SIGTERM)                  # the restore begins — and is held open
    _wait_for_the_restore_to_open(tmp_path / "restore-paused")
    proc.send_signal(signal.SIGTERM)                  # the second signal, inside the window
    (tmp_path / "restore-go").write_text("go")        # let the first restore finish if it still can

    _assert_untouched(source, proc, signal.SIGTERM, mode, tmp_path)


def test_a_restore_that_cannot_be_proved_keeps_the_record_and_the_journal(tmp_path, monkeypatch):
    """The other half of P1: keeping the record is only worth anything if it is kept on FAILURE.

    "Retain the recoverable state until the write has succeeded and been verified" has two ways to
    be wrong, and the second one is the quiet one. Dropping the record on a restore that did not
    land would leave a tree holding a mutant, no in-memory copy of what belongs there, and — if the
    journal went with it — nothing for the next run either. So this holds the write to failing and
    requires everything to still be where it was.

    The write is made to fail at `os.replace`, which is the step that makes the staged bytes the
    file: everything before it succeeds and the bytes on disk are still the mutant, which is exactly
    the state a verification exists to notice.
    """
    module = _module()
    target = tmp_path / "src.py"
    mutant = b"guard = False\n"                  # what the mutation left in the tree
    pristine = b"guard = True\n"                 # what has to come back
    target.write_bytes(mutant)
    mode = stat.S_IMODE(target.stat().st_mode)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module.os, "replace", lambda *args, **kwargs: None)
    journal = tmp_path / JOURNAL
    journal.write_text("{}", encoding="utf-8")
    module._in_flight = module._InFlight(target, pristine, mode)

    try:
        module._restore_now()
    except module._RestoreFailed:
        pass
    else:
        raise AssertionError("a restore that never landed was reported as a success")

    assert target.read_bytes() == mutant, "the probe changed the file it was checking"
    assert module._in_flight is not None, (
        "the in-memory record was dropped for a restore that did not land, so the next attempt and "
        "the `atexit` backstop both have nothing to work from")
    assert journal.exists(), "the journal was dropped for a restore that did not land"
    assert not list(tmp_path.glob("*" + module.RESTORE_STAGING)), (
        "the failed restore left its staging file beside the target")


def test_a_planted_journal_is_recovered_before_the_run_reads_anything(tmp_path):
    """The SIGKILL case, which no handler can cover, tested from the far end of it.

    The journal's on-disk format is pinned here rather than produced by a helper: a journal written
    by an OLDER run has to still be readable, so the format is a contract and this is where it is
    written down. `mode` is 0o640 in the record and 0o640 is what the file must end up with — the
    copy in the journal is what survives, so the mode has to survive with it.
    """
    source, driver = _plant_tree(tmp_path, sleep=0)
    source.write_bytes(MUTANT)                        # what a SIGKILLed run leaves behind
    journal = tmp_path / JOURNAL
    journal.write_text(json.dumps({
        "path": "src.py",
        "sha256": hashlib.sha256(PRISTINE).hexdigest(),
        "mode": 0o640,
        "bytes": base64.b64encode(PRISTINE).decode("ascii"),
    }), encoding="utf-8")

    proc = subprocess.run([sys.executable, str(driver)], cwd=tmp_path, capture_output=True,
                          text=True, timeout=120)

    assert source.read_bytes() == PRISTINE, (
        f"the run mutated a tree it had not repaired first: {source.read_bytes()!r}")
    assert "RECOVERED src.py" in proc.stdout, (
        "the recovery was silent; a reader has to be told a killed run was undone\n"
        f"stdout={proc.stdout}")
    assert not journal.exists(), "the journal outlived the recovery it was used for"
    assert stat.S_IMODE(source.stat().st_mode) == 0o640, "the mode recorded in the journal was lost"


def test_a_sigkilled_run_is_undone_by_the_next_one(tmp_path):
    """End to end, with a real SIGKILL: the journal has to be written by the runner, not by a test.

    The planted-journal test above proves the RECOVERY works. It cannot prove the runner writes a
    journal in the first place, or that what it writes is what `_recover_from_journal` reads —
    two halves that can each be right while the pair does nothing. This kills a run for real, with
    the one signal that runs no handler and no `finally`, and then asks the next run to clean up.
    """
    source, driver = _plant_tree(tmp_path, sleep=3)
    proc = _start(tmp_path)
    _wait_for_mutation(proc, source)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)

    assert source.read_bytes() != PRISTINE, "the run did not get as far as mutating anything"
    assert (tmp_path / JOURNAL).exists(), (
        "SIGKILL runs no handler and no `finally`, so a journal written BEFORE the mutation is the "
        "only thing that can record what to put back — and there is none")

    _plant_test(tmp_path, sleep=0)          # the control is not what this test is about
    second = subprocess.run([sys.executable, str(driver)], cwd=tmp_path, capture_output=True,
                            text=True, timeout=120)

    assert source.read_bytes() == PRISTINE, (
        f"the run after the killed one did not repair the tree: {source.read_bytes()!r}")
    assert "RECOVERED src.py" in second.stdout, second.stdout
