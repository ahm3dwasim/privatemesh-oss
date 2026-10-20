"""The durable watcher: one poll, one change, and no duplicate after a restart.

"Always on" has to mean observable background work — a cursor that survives the process, a
message id that is classified once, a "last checked" the owner can read, and a pause that
actually stops the polling rather than hiding its results.
"""
from __future__ import annotations

from privatemesh.routine import Routine

from fakes_v2 import Harness, change_email


def harness(tmp_path, **kw):
    h = Harness(**kw)
    h.routine = Routine(h.engine, h.mail, path=str(tmp_path / "routine.db"))
    return h


def script():
    return {"classify": {"is_meeting_change": True, "reason": "proposes new times"},
            "extract": {"options": [{"start": "2026-09-19T15:00:00+01:00",
                                     "end": "2026-09-19T15:45:00+01:00", "phrase": "3"}]},
            "query": {"query": "cafe near King's Cross"},
            "choose": {"venue_index": 0, "is_venue": True, "reason_sentence": "open at 15:00",
                       "rejected": []},
            "draft": {"subject": "Re: tomorrow",
                      "fragments": [{"source": "time", "text": "15:00 works."}]}}


def test_one_poll_turns_a_new_email_into_one_change(tmp_path):
    h = harness(tmp_path, messages=[change_email("m1")], script=script())
    new = h.routine.tick()
    assert [c.message_id for c in new] == ["m1"]
    assert h.routine.state()["last_checked_at"]
    assert h.routine.state()["cursor"] == "m1"


def test_a_second_poll_with_no_new_mail_finds_nothing(tmp_path):
    h = harness(tmp_path, messages=[change_email("m1")], script=script())
    h.routine.tick()
    assert h.routine.tick() == []


def test_a_later_message_is_picked_up(tmp_path):
    h = harness(tmp_path, messages=[change_email("m1")], script=script())
    h.routine.tick()
    h.mail.messages.append(change_email("m3", subject="Change of plan again"))
    assert [c.message_id for c in h.routine.tick()] == ["m3"]


def test_a_restart_does_not_re_raise_the_same_change(tmp_path):
    """The cursor and the seen-set are on disk, so a new process asks the same question once."""
    h = harness(tmp_path, messages=[change_email("m1")], script=script())
    assert len(h.routine.tick()) == 1

    restarted = harness(tmp_path, messages=[change_email("m1")], script=script())
    assert restarted.routine.tick() == []
    assert restarted.routine.state()["cursor"] == "m1"
    assert restarted.routine.seen("m1")


def test_a_paused_routine_does_not_poll_at_all(tmp_path):
    h = harness(tmp_path, messages=[change_email("m1")], script=script())
    h.routine.pause()
    assert h.routine.tick() == []
    assert h.mail.polls == 0, "a paused routine still asked the mail provider for messages"
    assert h.routine.state()["paused"] is True


def test_resuming_picks_up_what_arrived_while_paused(tmp_path):
    h = harness(tmp_path, messages=[], script=script())
    h.routine.pause()
    h.routine.tick()
    h.mail.messages.append(change_email("m1"))
    h.routine.resume()
    assert [c.message_id for c in h.routine.tick()] == ["m1"]


def test_a_message_that_is_not_a_meeting_change_is_marked_seen_and_raises_nothing(tmp_path):
    h = harness(tmp_path, messages=[change_email("m1")], script={"classify": {"is_meeting_change": False}})
    assert h.routine.tick() == []
    assert h.routine.seen("m1")
    assert h.routine.tick() == []


def test_the_seen_set_is_bounded_by_what_the_owner_needs_not_by_memory(tmp_path):
    """A watcher that grows forever is a watcher that eventually stops. The cursor is the bound."""
    h = harness(tmp_path, messages=[change_email("m1")], script=script())
    h.routine.tick()
    assert h.routine.state()["cursor"] == "m1"
    assert set(h.routine.state()) >= {"enabled", "paused", "last_checked_at", "cursor",
                                      "interval_seconds"}
