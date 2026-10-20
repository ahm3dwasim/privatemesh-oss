"""An invite update whose notification setting is missing must NOT notify everyone.

Review of limits r2 (Medium): the calendar effect defaulted `send_updates` to "all", so a bundle
that lost the key on its way to the executor emailed every guest. A missing value is an unknown,
and an unknown external effect is refused as silence: "none"."""
from __future__ import annotations

from privatemesh import skills
from privatemesh.execute import build_effect_skills


class _Cal:
    def __init__(self):
        self.calls = []

    def update_event(self, event_id, etag, *, start, end, location, send_updates,
                     timezone=""):
        self.calls.append(send_updates)
        raise RuntimeError("stop after recording")


def test_a_missing_notification_setting_notifies_nobody():
    cal = _Cal()
    tool = next(t for t in build_effect_skills(cal, mail=None) if "calendar" in t.name)
    # Run as the broker would, so the test reaches the provider call rather than the bypass guard.
    token = skills._ACTIVE.set(skills._BROKER_TOKEN)
    try:
        tool.fn({"event_id": "e1", "if_match_etag": '"1"', "start": "2026-09-19T15:00:00+01:00",
                 "end": "2026-09-19T15:45:00+01:00", "location": "x"})
    except Exception:
        pass
    finally:
        skills._ACTIVE.reset(token)
    assert cal.calls == ["none"], cal.calls
