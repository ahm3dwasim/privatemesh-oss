"""The clock switch itself: what it freezes, what it refuses to touch, and what it must not do.

`fakeclock` patches `datetime` process-wide, so nothing here installs it in-process. The tests that
need an installed clock run one in a SUBPROCESS — the same way a reviewer runs the sweep — which
also means they exercise `install_from_env` exactly as `conftest.py` calls it rather than a
reconstruction of it. The rest drive `FrozenDateTime` directly through `monkeypatch`, which is
restored for every other test in the suite.

The point of the file is the second half. A clock switch that quietly changed the language every
other test is written against would be worse than the time bomb it exists to find.
"""
from __future__ import annotations

import datetime as _dt
import os
import subprocess
import sys
from pathlib import Path

from fakeclock import ENV, FrozenDateTime, parse

TESTS = Path(__file__).resolve().parent

FROZEN = "2026-12-10T12:00:00Z"


def probe(code: str, *, fake_now: str | None = None) -> list[str]:
    """Run `code` in a fresh interpreter with `PM_FAKE_NOW` set to `fake_now` (or removed)."""
    env = dict(os.environ)
    env.pop(ENV, None)
    if fake_now is not None:
        env[ENV] = fake_now
    env["PYTHONPATH"] = os.pathsep.join([str(TESTS), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                          cwd=str(TESTS.parent))
    assert done.returncode == 0, done.stderr
    return done.stdout.strip().splitlines()


# ---------------------------------------------------------------- the switch -------------------
def test_the_switch_is_inert_when_the_variable_is_unset():
    """The ordinary suite must be untouched: no variable, no patch, nothing frozen."""
    installed, still_real = probe(
        "import datetime as dt\n"
        "import fakeclock\n"
        "print(fakeclock.install_from_env())\n"
        "print(dt.datetime is fakeclock.REAL_DATETIME)\n")
    assert installed == "None", "an unset variable must not install a clock"
    assert still_real == "True", "datetime was replaced with the switch off"


def test_the_variable_freezes_now_utcnow_and_today():
    installed, now_utc, utcnow, today = probe(
        "import datetime as dt\n"
        "import fakeclock\n"
        "print(fakeclock.install_from_env())\n"
        "print(dt.datetime.now(dt.timezone.utc).isoformat())\n"
        "print(dt.datetime.utcnow().isoformat())\n"
        "print(dt.datetime.today().date().isoformat())\n",
        fake_now=FROZEN)
    assert installed == "2026-12-10 12:00:00+00:00"
    assert now_utc == "2026-12-10T12:00:00+00:00"
    assert utcnow == "2026-12-10T12:00:00"      # naive UTC, as the real `utcnow` is
    assert today == "2026-12-10"


def test_a_naive_instant_is_read_as_utc_not_as_local_time():
    """Guessing the machine's zone is how a "pinned" clock ends up moving with the machine."""
    assert parse("2026-12-10T12:00:00").utcoffset() == _dt.timedelta(0)


def test_an_aware_instant_keeps_its_own_offset():
    assert parse("2026-09-19T09:00:00+01:00").utcoffset() == _dt.timedelta(hours=1)


# ---------------------------------------------------------------- what it must not change ------
def test_the_frozen_class_is_still_a_datetime(monkeypatch):
    """`isinstance(now(), datetime)` has to stay True for the class that was just installed.

    A `now()` that built a plain `datetime` — which is what `astimezone` on a real datetime hands
    back — would answer False here, turning the fix into a subtler version of the bug.
    """
    monkeypatch.setattr(FrozenDateTime, "_frozen", parse(FROZEN))
    moment = FrozenDateTime.now(_dt.timezone.utc)
    assert type(moment) is FrozenDateTime
    assert isinstance(moment, FrozenDateTime)
    assert isinstance(moment, _dt.datetime)     # and it is still a datetime in every other sense
    assert isinstance(moment, _dt.date)         # ...including the base class it always was


def test_now_without_a_zone_is_naive_local_time(monkeypatch):
    """`datetime.now()` with no argument returns naive LOCAL time; the fake keeps that shape."""
    monkeypatch.setattr(FrozenDateTime, "_frozen", parse(FROZEN))
    naive = FrozenDateTime.now()
    assert naive.tzinfo is None
    assert naive.utcoffset() is None
    # The same instant the real `datetime.now()` would show if the wall clock read `FROZEN`.
    assert naive == parse(FROZEN).astimezone().replace(tzinfo=None)


def test_date_is_deliberately_left_alone():
    """`datetime.date` must still be the real class, and `fakeclock` must not have replaced it.

    Patching it would make `isinstance(x, datetime.date)` False for the real `date` that
    `.date()` returns — a change to the language in exchange for freezing `date.today()`, which
    nothing in this repo calls. pydantic v1 also builds `class ConstrainedDate(date, ...)` at
    import time, so a custom metaclass on `date` is a hard `TypeError` at collection.
    """
    import fakeclock
    assert not hasattr(fakeclock, "FrozenDate"), (
        "a FrozenDate is back — see the module docstring for why it was removed")
    assert isinstance(_dt.date.today(), _dt.date)
    assert type(_dt.datetime.now().date()) is _dt.date


def test_installing_the_switch_does_not_replace_date():
    """The same boundary, asserted through the real install path rather than by inspecting it."""
    date_type, is_date, from_datetime = probe(
        "import datetime as dt\n"
        "import fakeclock\n"
        "fakeclock.install_from_env()\n"
        "print(type(dt.date.today()).__name__)\n"
        "print(isinstance(dt.date.today(), dt.date))\n"
        "print(type(dt.datetime.now().date()).__name__)\n",
        fake_now=FROZEN)
    assert date_type == "date"
    assert is_date == "True"
    assert from_datetime == "date"
