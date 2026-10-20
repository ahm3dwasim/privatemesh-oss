"""The suite must not inherit the operator's credentials, whatever directory it runs from.

`Settings.load()` copies a `.env` beside the process into `os.environ` with `setdefault`. Run from
the owner's own checkout, that used to put the real `NEBIUS_API_KEY` and `TAVILY_API_KEY` into the
test process — permanently, from whichever test built `Settings` first — so a test that reached a
provider would have reached the real one, and the suite's verdict depended on the directory it ran
in. The demo tests found it: demo mode refuses to start beside a real provider credential, so 48 of
them went red in the owner's tree and green in every worktree that had no `.env`.

`conftest` now points `PM_ENV_FILE` at a path that does not exist and clears those names. These
tests are what stops that quietly coming undone.
"""
from __future__ import annotations

import os
from pathlib import Path

from privatemesh import config
from privatemesh.config import Settings

#: Every name a `.env` could set that decides whether this process can reach a real provider.
CREDENTIAL_NAMES = ("NEBIUS_API_KEY", "TAVILY_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")


def test_the_suite_runs_with_no_provider_credential_in_its_environment():
    for name in CREDENTIAL_NAMES:
        assert not os.environ.get(name), (
            f"{name} is set during the test run: a test that reached a provider would reach the "
            f"real one. See tests/conftest.py.")


def test_the_env_file_the_suite_points_at_does_not_exist():
    named = os.environ.get(config.ENV_FILE_VAR)
    assert named, "conftest must name an env file, so no test falls back to ./.env"
    assert not Path(named).exists(), f"{named} exists, so the suite is loading a real env file"


def test_a_dot_env_in_the_working_directory_is_not_read(tmp_path, monkeypatch):
    """The counterfactual for the line above: with `PM_ENV_FILE` elsewhere, a `.env` sitting right
    beside the process is ignored — which is the whole point, since that is the file the owner's
    real keys live in."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TAVILY_API_KEY=tvly-from-the-working-directory\n")
    monkeypatch.setenv(config.ENV_FILE_VAR, str(tmp_path / "nothing-here.env"))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert Settings.load().tavily_key == ""


def test_the_named_env_file_IS_read_so_the_isolation_is_not_vacuous(tmp_path, monkeypatch):
    """The control. If `_load_dotenv` had simply stopped working, the test above would pass for the
    wrong reason and an operator's real `.env` would stop being read in production too."""
    monkeypatch.chdir(tmp_path)
    named = tmp_path / "named.env"
    named.write_text("TAVILY_API_KEY=tvly-from-the-named-file\n")
    monkeypatch.setenv(config.ENV_FILE_VAR, str(named))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert Settings.load().tavily_key == "tvly-from-the-named-file"


def test_an_explicit_path_still_wins_over_the_variable(tmp_path, monkeypatch):
    """`_load_dotenv(path)` keeps its old meaning: the variable is the DEFAULT, not an override of
    a caller who named a file."""
    monkeypatch.setenv(config.ENV_FILE_VAR, str(tmp_path / "ignored.env"))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    explicit = tmp_path / "explicit.env"
    explicit.write_text("TAVILY_API_KEY=tvly-explicit\n")
    config._load_dotenv(explicit)
    assert os.environ["TAVILY_API_KEY"] == "tvly-explicit"


def test_no_default_state_path_resolves_into_the_operators_real_home(monkeypatch):
    """Every per-user default the product computes must land in the suite's temporary home. The demo
    ledger escaped this once, because the conftest fixture named variables one at a time and a new
    one was added without it; `HOME` is now redirected too, so a default nobody has listed yet
    still cannot reach the owner's files. The ledgers are BUILT here with their variables removed,
    so what is checked is the path the product would really open, not a string this test typed."""
    from pathlib import Path

    from privatemesh.demo_session import DemoSessions
    from privatemesh.spend import SpendLedger

    home = Path.home().resolve()
    assert "pm-home" in str(home), f"the suite is running with the operator's real home: {home}"
    for name in ("PM_DEMO_LEDGER", "PM_SPEND_LEDGER", "PM_OWNER_TOKEN_FILE", "PM_GOOGLE_TOKEN_DIR"):
        assert "pm-home" in os.environ[name], f"{name} points outside the suite's home"

    monkeypatch.delenv("PM_SPEND_LEDGER")
    monkeypatch.delenv("PM_DEMO_LEDGER")
    assert Path(SpendLedger().path).resolve().is_relative_to(home)
    sessions = DemoSessions(settings=None, world_builder=lambda **kw: None)
    assert Path(sessions.global_ledger.path).resolve().is_relative_to(home)


def test_the_google_client_default_follows_the_checkout_not_a_fixed_dev_machine_path():
    """The default was once hard-coded to one developer's folder layout, true only while the
    checkout sat at that exact place. Derived from the package's own location, it moves with the
    checkout — so the demo's credential refusal keeps seeing a real secret wherever the project
    lives."""
    from pathlib import Path

    from privatemesh import google_auth

    root = Path(google_auth.__file__).resolve().parents[2]
    assert (root / "src" / "privatemesh" / "google_auth.py").exists(), root
    assert Path(google_auth.DEFAULT_CLIENT_PATH) == root / "secrets" / "google_client.json"
