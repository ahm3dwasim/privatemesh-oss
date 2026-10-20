"""The Contree backend, exercised against a fake that behaves like the service's contract.

`contree-sdk` is not installed here and the owner's Token Factory key currently returns
ForbiddenError — sandboxes are behind beta access — so this lane makes no network calls and cannot
run the real thing. What it CAN do is hold `ContreeRunner` to the protocol it will use: upload the
skill, the bootstrap and the job; execute it with the sandbox's own paths; read the result BEFORE
destroying the sandbox; and report the same `RunResult` the local backend does.

The fake is not a mock of the SDK's API surface — nobody here has run that API, and inventing its
shape would be a guess dressed as a test. It implements the narrow `ContreeClient` protocol this
repo declares, and it actually executes the uploaded files, so the runner's sequencing (upload →
exec → read → destroy) is genuinely exercised rather than asserted.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from privatemesh.skillrt.runner import ContreeRunner, Limits, RunSpec

BOOTSTRAP_SRC = Path(__file__).resolve().parents[1] / "src" / "privatemesh" / "skillrt" / "_bootstrap.py"

SKILL = """
    def run(inputs):
        print("computing")
        return {"ranked": sorted(inputs["slots"])}
"""


@dataclass
class FakeExec:
    exit_code: int
    stdout: str
    stderr: str


class FakeSandbox:
    """A sandbox on the local filesystem, with the service's path conventions.

    `/skill`, `/scratch` and `/job.json` are absolute inside a real sandbox; here they are mapped
    under a temp root, which is exactly what a mount would do. Any `exec` after `destroy` raises —
    the same reason the real runner must read its result before tearing the box down.
    """

    def __init__(self, root: Path, image: str) -> None:
        self.root = root
        self.image = image
        self.destroyed = False
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.files: dict[str, bytes] = {}

    def _local(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def _map(self, arg: str) -> str:
        """An absolute sandbox path becomes the same path under the temp root. Flags (`-I`, `-S`)
        and the interpreter name are left alone — they are not filesystem paths."""
        if arg.startswith("path:"):
            return str(self._local(arg[len("path:"):]))
        return str(self._local(arg)) if arg.startswith("/") else arg

    def upload(self, files: dict[str, bytes]) -> None:
        assert not self.destroyed
        self.files = dict(files)
        for path, data in files.items():
            target = self._local(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        # A real sandbox has the scratch mount present before the process starts.
        self._local("/scratch").mkdir(parents=True, exist_ok=True)
        # Point the job at where the mounts actually landed.
        job_path = self._local("/job.json")
        job = json.loads(job_path.read_text(encoding="utf-8"))
        job["skill_dir"] = str(self._local("/skill"))
        job["scratch_dir"] = str(self._local("/scratch"))
        job["read_roots"] = [str(self._local(p)) for p in job.get("read_roots") or []]
        job_path.write_text(json.dumps(job), encoding="utf-8")

    def exec(self, argv, *, env, timeout_s) -> FakeExec:
        assert not self.destroyed, "exec after destroy: the runner tore the sandbox down too early"
        self.calls.append(list(argv))
        self.envs.append(dict(env))
        if argv[0] == "cat":
            target = self._local(argv[1])
            return FakeExec(0, target.read_text(encoding="utf-8") if target.exists() else "", "")
        resolved = [self._map(a) for a in argv[1:]]
        proc = subprocess.run([sys.executable, *resolved], capture_output=True, text=True,
                              timeout=timeout_s, cwd=str(self._local("/scratch")), env=env)
        return FakeExec(proc.returncode, proc.stdout, proc.stderr)

    def destroy(self) -> None:
        self.destroyed = True
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)


class FakeContree:
    """The `ContreeClient` protocol. `base_url` is the documented one, so a test that swaps this for
    the real client is swapping one object."""

    base_url = "https://api.tokenfactory.nebius.com/sandboxes/"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sandboxes: list[FakeSandbox] = []

    def create_sandbox(self, *, image: str, timeout_s: int) -> FakeSandbox:
        sandbox = FakeSandbox(self.root / f"box{len(self.sandboxes)}", image)
        self.sandboxes.append(sandbox)
        return sandbox


@pytest.fixture
def fake(tmp_path):
    return FakeContree(tmp_path / "sandboxes")


def write_skill(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "rank_slots.py").write_text(textwrap.dedent(SKILL), encoding="utf-8")
    return root


def test_the_contree_backend_runs_a_skill_end_to_end(fake, tmp_path):
    skill = write_skill(tmp_path / "skill")
    result = ContreeRunner(fake).run(RunSpec(skill_dir=skill, module="rank_slots", entry="run",
                                             inputs={"slots": ["16:00", "15:00"]},
                                             limits=Limits(wall_seconds=60)))

    assert result.status == "ok", result.error
    assert result.result == {"ranked": ["15:00", "16:00"]}
    assert result.stdout.strip() == "computing"
    assert result.backend == "contree"


def test_the_sandbox_is_destroyed_and_the_result_is_read_first(fake, tmp_path):
    """Ordering is the bug this backend is most likely to ship with: tear the box down and the
    result goes with it. The fake raises on `exec` after `destroy`, so a reversal fails loudly."""
    skill = write_skill(tmp_path / "skill")
    ContreeRunner(fake).run(RunSpec(skill_dir=skill, module="rank_slots", entry="run",
                                    inputs={"slots": []}, limits=Limits(wall_seconds=60)))
    assert fake.sandboxes and all(s.destroyed for s in fake.sandboxes)
    assert ["cat", "/result.json"] in fake.sandboxes[0].calls, \
        "the result must be read while the sandbox still exists"


def test_the_upload_carries_the_bootstrap_the_skill_and_the_job(fake, tmp_path):
    skill = write_skill(tmp_path / "skill")
    result = ContreeRunner(fake).run(RunSpec(skill_dir=skill, module="rank_slots", entry="run",
                                             inputs={"slots": []}, limits=Limits(wall_seconds=60)))
    uploads = result.isolation["uploads"]
    assert "/skillrt/_bootstrap.py" in uploads
    assert "/skill/rank_slots.py" in uploads
    assert "/job.json" in uploads


def test_a_held_out_directory_is_uploaded_to_its_own_root(fake, tmp_path):
    """The harness's revocation depends on the held-out tests being a separate root inside the
    sandbox too, not mixed into the skill's directory."""
    skill = write_skill(tmp_path / "skill")
    held = tmp_path / "held_out"
    held.mkdir()
    (held / "test_held.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    result = ContreeRunner(fake).run(RunSpec(skill_dir=skill, module="rank_slots", entry="run",
                                             inputs={"slots": []}, read_roots=(held,),
                                             limits=Limits(wall_seconds=60)))
    assert "/heldout/0/test_held.py" in result.isolation["uploads"]
    assert not any(u.startswith("/skill/held") for u in result.isolation["uploads"])


def test_the_contree_report_names_the_boundary_it_is_relying_on(fake, tmp_path):
    """The value over the local runner is that isolation stops being Python-level, and the report
    has to say so — otherwise the two backends' evidence looks interchangeable when it is not."""
    skill = write_skill(tmp_path / "skill")
    result = ContreeRunner(fake).run(RunSpec(skill_dir=skill, module="rank_slots", entry="run",
                                             inputs={"slots": []}, limits=Limits(wall_seconds=60)))
    assert result.isolation["env_inherited"] is False
    assert result.isolation["base_url"] == "https://api.tokenfactory.nebius.com/sandboxes/"
    assert "separate machine" in result.isolation["vm_boundary"]


def test_a_sandbox_that_returns_nothing_is_an_error_not_a_pass(fake, tmp_path):
    """A backend that cannot report must not be read as one that reported success."""
    skill = write_skill(tmp_path / "skill")

    class Mute(FakeContree):
        def create_sandbox(self, *, image, timeout_s):
            sandbox = super().create_sandbox(image=image, timeout_s=timeout_s)
            sandbox.exec = lambda argv, **kw: FakeExec(0, "", "")     # never produces a result
            return sandbox

    mute = Mute(fake.root)
    result = ContreeRunner(mute).run(RunSpec(skill_dir=skill, module="rank_slots", entry="run",
                                             inputs={"slots": []}, limits=Limits(wall_seconds=60)))
    assert result.status == "error"
    assert result.ok is False
    assert "no result from the sandbox" in result.error


def test_the_bytes_uploaded_are_the_guard_in_this_repo(fake, tmp_path):
    """Byte-for-byte. A sandbox that silently ran a stale or different copy of the enforcement code
    would produce a result indistinguishable from a guarded one."""
    skill = write_skill(tmp_path / "skill")
    ContreeRunner(fake).run(RunSpec(skill_dir=skill, module="rank_slots", entry="run",
                                    inputs={"slots": []}, limits=Limits(wall_seconds=60)))
    uploaded = fake.sandboxes[0].files["/skillrt/_bootstrap.py"]
    assert uploaded == BOOTSTRAP_SRC.read_bytes()


def test_the_environment_handed_to_the_sandbox_is_empty(fake, tmp_path):
    """Nothing inherited means nothing to leak: no owner token, no HOME, no cloud credentials."""
    skill = write_skill(tmp_path / "skill")
    ContreeRunner(fake).run(RunSpec(skill_dir=skill, module="rank_slots", entry="run",
                                    inputs={"slots": []}, limits=Limits(wall_seconds=60)))
    assert fake.sandboxes[0].envs == [{}, {}]
