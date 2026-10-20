"""`scripts/openshell_run.sh` — the refusal paths, and the regressions the real run of 2026-09-21
taught (docs/NVIDIA_STACK.md, "What running it found").

This machine now HAS `openshell` on PATH (a real, running gateway), so
`test_it_refuses_with_an_actionable_message_when_openshell_is_not_on_path` exercises the refusal
with a PATH built to exclude it rather than by assuming absence — the one thing this file can no
longer take for granted. Every other refusal test in here — the archive-secrets check, the
unrecognised-policy check — never touches the real `openshell` binary: a fake one on PATH is enough
to get past the first `command -v` check, and the script must refuse before calling it for anything
real.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "openshell_run.sh"
PROBE = ROOT / "scripts" / "openshell_egress_probe.py"


def _run(*, path: str, cwd: Path | None = None, args: list[str] | None = None
         ) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *(args or [])], cwd=cwd or ROOT,
                          capture_output=True, text=True, env={"PATH": path})


def _fake_openshell(bin_dir: Path, *, log: Path) -> None:
    """A stand-in `openshell` that only ever records what it was called with and exits 0 — enough
    to satisfy `command -v openshell`, and never enough to fake a real sandbox run. Any test that
    reaches this is a test asserting the script refused BEFORE doing anything real."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "openshell"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 0\n')
    stub.chmod(0o755)


def test_it_is_executable_and_present():
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111, "openshell_run.sh must be executable"


def test_the_egress_probe_is_present_alongside_it():
    assert PROBE.exists()
    assert PROBE.stat().st_mode & 0o111, "openshell_egress_probe.py must be executable"


def test_it_refuses_with_an_actionable_message_when_openshell_is_not_on_path(tmp_path):
    # A PATH with real coreutils (cd, cat, command, trap, git) but no `openshell` anywhere on it —
    # the exact absence the script must detect rather than fail on cryptically. Built from `which`
    # rather than a hard-coded `/usr/bin:/bin`, because this machine's real `openshell` and `git`
    # do not both live there.
    git_dir = str(Path(shutil.which("git")).parent)
    result = _run(path=f"/usr/bin:/bin:{git_dir}")
    assert result.returncode == 1
    assert "not on PATH" in result.stderr
    assert "install.sh" in result.stderr, "the refusal names the real install command to read"


def test_it_never_pipes_an_installer_to_a_shell_itself():
    text = SCRIPT.read_text(encoding="utf-8")
    # The install command is quoted TEXT for the operator to read, inside a `cat <<'EOF'` block
    # (single-quoted heredoc, so nothing in it is executed) — never something this script runs.
    assert "| sh" not in text.split("cat >&2")[0] + text.split("EOF")[-1]
    assert "curl" not in (text.split("cat >&2 <<'EOF'")[0] if "cat >&2 <<'EOF'" in text else text)


def test_it_never_bare_calls_brew_pip_npm_apt():
    text = SCRIPT.read_text(encoding="utf-8")
    for tool in ("brew install", "pip install", "npm install", "apt-get install", "apt install"):
        assert tool not in text


def test_it_refuses_an_unrecognised_policy_file(tmp_path):
    """The egress probe's `EXPECTED` table is keyed to `product`/`demo`; a policy file this script
    cannot name one of those two for must be refused rather than silently probed against the wrong
    expectations."""
    log = tmp_path / "openshell.log"
    _fake_openshell(tmp_path / "bin", log=log)
    other = tmp_path / "some-other-policy.yaml"
    other.write_text("version: 1\n")
    result = _run(path=f"{tmp_path / 'bin'}:/usr/bin:/bin", cwd=ROOT, args=[str(other)])
    assert result.returncode == 1
    assert "unrecognised policy file" in result.stderr
    assert not log.exists() or "sandbox create" not in log.read_text(), (
        "must refuse before ever creating a sandbox")


def _init_repo_with_tracked_secret(tmp_path: Path, *, secret_path: str) -> Path:
    """A minimal git repo shaped enough for the script's own archive paths to exist, carrying one
    tracked secret at `secret_path` — everything `openshell_run.sh` needs to reach the archive
    check and nothing it needs to run for real past it."""
    repo = tmp_path / "repo"
    for d in ("src", "redteam", "deploy/openshell", "scripts", "tests"):
        (repo / d).mkdir(parents=True, exist_ok=True)
        # `git archive HEAD -- <path>` fails on a pathspec matching nothing tracked, and git does
        # not track empty directories — a placeholder keeps every archived path non-empty.
        (repo / d / ".keep").write_text("")
    (repo / "pyproject.toml").write_text("[project]\nname = \"x\"\n")
    (repo / "deploy/openshell/privatemesh.policy.yaml").write_text(
        (ROOT / "deploy/openshell/privatemesh.policy.yaml").read_text())
    (repo / "deploy/openshell/demo.policy.yaml").write_text(
        (ROOT / "deploy/openshell/demo.policy.yaml").read_text())
    shutil.copy(SCRIPT, repo / "scripts" / "openshell_run.sh")
    shutil.copy(PROBE, repo / "scripts" / "openshell_egress_probe.py")
    secret = repo / secret_path
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("NEBIUS_API_KEY=not-a-real-key\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _run_copied_script(repo: Path, *, path: str) -> subprocess.CompletedProcess:
    """Run the SCRIPT COPIED INTO `repo` (not the real one) — `openshell_run.sh` derives its own
    repo root from its own location (`$SCRIPT_DIR/..`), so exercising the archive check against a
    throwaway repo means running the copy that actually lives inside it."""
    return subprocess.run(["bash", str(repo / "scripts" / "openshell_run.sh")], cwd=repo,
                          capture_output=True, text=True, env={"PATH": path})


def test_it_refuses_when_the_archived_tree_carries_a_tracked_dotenv(tmp_path):
    repo = _init_repo_with_tracked_secret(tmp_path, secret_path="src/.env")
    log = tmp_path / "openshell.log"
    _fake_openshell(tmp_path / "bin", log=log)
    result = _run_copied_script(repo, path=f"{tmp_path / 'bin'}:/usr/bin:/bin")
    assert result.returncode == 1
    assert ".env" in result.stderr and "secrets/" in result.stderr
    assert not log.exists() or "sandbox create" not in log.read_text()


def test_it_refuses_when_the_archived_tree_carries_a_tracked_secrets_path(tmp_path):
    repo = _init_repo_with_tracked_secret(tmp_path, secret_path="deploy/secrets/google_client.json")
    log = tmp_path / "openshell.log"
    _fake_openshell(tmp_path / "bin", log=log)
    result = _run_copied_script(repo, path=f"{tmp_path / 'bin'}:/usr/bin:/bin")
    assert result.returncode == 1
    assert "secrets/" in result.stderr
    assert not log.exists() or "sandbox create" not in log.read_text()


# ---------------------------------------------------------- regressions the real run surfaced ---
def test_every_exec_and_unpack_call_uses_the_absolute_system_python():
    """Bare `python3` resolves to the sandbox image's OWN `.venv` (Python 3.14, via a `python3 ->
    python` symlink) rather than the system `/usr/bin/python3` (3.12) our cp312 wheels target —
    measured today (docs/NVIDIA_STACK.md, "What running it found"). Every `python3` this script
    invokes INSIDE the sandbox must be the explicit absolute path; `python3 -m pip download` on
    the HOST is fine and deliberately excluded."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "pip download" in line:
            continue
        assert not re_search_bare_python3(line), (
            f"bare `python3` (not /usr/bin/python3) in a sandbox-facing line: {line!r}")


def re_search_bare_python3(line: str) -> bool:
    import re

    return bool(re.search(r"(?<!/usr/bin/)(?<!\w)python3\b", line)) and "python3 -m pip" not in line


def test_directory_uploads_never_pass_a_dest_argument():
    """`sandbox upload NAME LOCAL_DIR DEST` always nests the directory under ITS OWN basename
    below `DEST` — measured today by running this script (docs/NVIDIA_STACK.md). The wheels
    directory is uploaded with no `DEST`, which is the only shape that lands its contents directly
    at the sandbox workdir."""
    text = SCRIPT.read_text(encoding="utf-8")
    upload_lines = [l for l in text.splitlines() if "sandbox upload" in l and not l.strip().startswith("#")]
    wheels_lines = [l for l in upload_lines if "wheels" in l]
    assert wheels_lines, "expected a `sandbox upload` line for the wheels directory"
    for line in wheels_lines:
        # After `"$WORK/wheels"` the line must end (module the `>&2` redirect) — no third,
        # DEST argument.
        assert line.rstrip().endswith('"$WORK/wheels" >&2') or line.rstrip().endswith('"$WORK/wheels"'), (
            f"wheels upload must not pass a DEST argument: {line!r}")


def test_probe_upload_never_passes_a_mismatched_dest():
    """Same rule, file form: a `DEST` that differs from the local file's own basename gets treated
    as a directory to nest the file under, not a rename — measured the same way (an earlier
    version of this script uploaded the probe to `egress_probe.py`, which is not its basename, and
    the exec step then failed with `can't find '__main__' module in '/sandbox/egress_probe.py'`,
    because that path was a DIRECTORY containing the real file)."""
    text = SCRIPT.read_text(encoding="utf-8")
    probe_upload = next(l for l in text.splitlines()
                        if "sandbox upload" in l and "openshell_egress_probe.py" in l
                        and not l.strip().startswith("#"))
    assert "egress_probe.py\"" not in probe_upload.replace("openshell_egress_probe.py", ""), (
        f"probe upload must not pass a mismatched DEST: {probe_upload!r}")
    exec_lines = [l for l in text.splitlines() if "python3 openshell_egress_probe.py" in l]
    assert exec_lines, "the probe must be invoked by its uploaded (matching-basename) filename"


def test_per_binary_enforcement_is_checked_as_its_own_top_level_exec_command():
    """`curl` run as a SUBPROCESS of the (granted) python3 probe reaches the network the parent
    was granted — measured today. Only `curl` as `sandbox exec`'s own command is actually checked
    against the policy's `binaries:` list, so that is how this script must invoke it: directly
    after `sandbox exec ... --`, not inside a `python3 -c` or a probe script's subprocess call."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "sandbox exec" in text and "-- \\\n    curl " in text, (
        "expected a `sandbox exec ... -- curl ...` invocation as its own command")
    probe_text = (ROOT / "scripts" / "openshell_egress_probe.py").read_text()
    assert "import subprocess" not in probe_text and "subprocess.run" not in probe_text, (
        "the egress probe must not shell out to curl itself — that no longer tests per-binary "
        "enforcement (see docs/NVIDIA_STACK.md)")


def test_it_always_deletes_the_sandbox_even_on_failure():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "trap cleanup EXIT" in text
    assert "sandbox delete" in text
