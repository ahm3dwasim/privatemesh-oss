"""A process-wide clock, so the suite can be run as if it were any day.

**Off unless `PM_FAKE_NOW` is set.** With the variable unset this module installs nothing and the
ordinary suite reads the real clock exactly as before; the switch exists so a reviewer can
re-run the sweep that found the hardcoded-date tests:

    PM_FAKE_NOW=2026-12-10T12:00:00Z .venv/bin/python -m pytest tests/ -q
    PM_FAKE_NOW=2027-03-01T03:00:00Z .venv/bin/python -m pytest tests/ -q

`tests/conftest.py` calls `install_from_env()` at the very top, BEFORE it imports anything from
`privatemesh`. That ordering is load-bearing: three modules (`sim`, `gmail`, `tavily_evidence`)
do `from datetime import datetime`, which binds the class at import time, so installing after
them would leave those three reading the real clock and the sweep would quietly miss them.

Why a switch at all: a test that reads the wall clock passes on the day it was written and fails
later, which is a failure that arrives with no code change behind it. The fix for each such test
is an injected clock (see `wiring.build_*`'s `now=`), and this module is what proves the fix
holds on days that have not happened yet.

What is patched, and what is deliberately not:

- **`datetime.datetime` only.** `date` is left alone. Patching it means `datetime.date` is no
  longer the class `.date()` returns for a real datetime, so `isinstance(x, datetime.date)` turns
  False — a change to the language every test is written against, in exchange for freezing
  `date.today()`, which nothing in this repo calls. `test_fakeclock.py` holds that boundary.
- **No metaclass.** pydantic v1 builds `class ConstrainedDate(date, metaclass=...)` at import
  time, so a custom metaclass on `date` is a `TypeError: metaclass conflict` for every test that
  touches a pydantic model. It cost 95 collection errors and bought nothing.
- **The fake returns its own type.** `FrozenDateTime.now()` builds a `FrozenDateTime`, not a plain
  `datetime`: if it returned the real class, `isinstance(datetime.now(), datetime.datetime)` would
  be False for the very class that was just installed, which is a worse lie than the one being
  fixed.
"""
from __future__ import annotations

import datetime as _dt
import os

#: The real class, captured before anything is replaced.
REAL_DATETIME = _dt.datetime

#: The environment variable that turns the switch on. An ISO-8601 instant.
ENV = "PM_FAKE_NOW"


def parse(text: str) -> REAL_DATETIME:
    """An aware instant from `text`. A naive string is read as UTC rather than as local time —
    guessing the machine's zone is how a "pinned" clock ends up moving with the machine."""
    moment = REAL_DATETIME.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment


class FrozenDateTime(REAL_DATETIME):
    """`datetime` with `now`/`utcnow`/`today` answering `_frozen` instead of the wall clock.

    Everything else — `fromisoformat`, arithmetic, comparison, `strftime` — is inherited, because
    the point is to move the clock, not to replace the type.
    """

    _frozen: REAL_DATETIME | None = None

    @classmethod
    def _same_type(cls, moment: REAL_DATETIME) -> "FrozenDateTime":
        """`moment` as `cls`, field for field. `astimezone` on a plain datetime hands back a plain
        datetime, which would make `isinstance(now(), datetime.datetime)` False."""
        return cls(moment.year, moment.month, moment.day, moment.hour, moment.minute,
                   moment.second, moment.microsecond, moment.tzinfo, fold=moment.fold)

    @classmethod
    def now(cls, tz=None) -> "FrozenDateTime":
        moment = cls._frozen
        if tz is None:
            # `datetime.now()` with no argument is naive LOCAL time; keep that shape.
            moment = moment.astimezone().replace(tzinfo=None)
        else:
            moment = moment.astimezone(tz)
        return cls._same_type(moment)

    @classmethod
    def utcnow(cls) -> "FrozenDateTime":
        return cls._same_type(cls._frozen.astimezone(_dt.timezone.utc).replace(tzinfo=None))

    @classmethod
    def today(cls) -> "FrozenDateTime":
        return cls.now()


def install(moment: REAL_DATETIME) -> None:
    FrozenDateTime._frozen = moment
    _dt.datetime = FrozenDateTime


def install_from_env() -> REAL_DATETIME | None:
    """Install the frozen clock if `PM_FAKE_NOW` asks for one. Returns the instant, or None."""
    text = os.environ.get(ENV)
    if not text:
        return None
    moment = parse(text)
    install(moment)
    return moment
