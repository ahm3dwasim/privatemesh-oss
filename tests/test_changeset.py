"""The versioned approval bundle: one review, two effects, and no way to spend a stale yes.

The claim is that approval is bound to EXACT arguments — an edit, a moved event or an edited
private constraint all invalidate it — and that each listed action is separately consumable, so
"approve the plan" never becomes "approve whatever runs next".
"""
from __future__ import annotations

import pytest

from privatemesh.approval import APPROVED, ApprovalError, ApprovalStore
from privatemesh.changeset import ChangeSetStore, StaleChangeSet, _calendar_display

from fakes_v2 import at

CAL_ARGS = {"event_id": "ev1", "if_match_etag": "etag-1", "start": at("15:00"),
            "end": at("15:45"), "location": "The Standard, King's Cross", "send_updates": "all"}
MAIL_ARGS = {"to": ["alex@example.com"], "subject": "Re: tomorrow",
             "body": "15:00 works for me.", "in_reply_to": "m2", "idempotency_key": "k1"}


@pytest.fixture
def store():
    approvals = ApprovalStore()
    s = ChangeSetStore(approvals)
    cal = approvals.request("task1", "calendar_update_event", CAL_ARGS)
    mail = approvals.request("task1", "email_reply", MAIL_ARGS)
    approvals.approve(cal.id)
    approvals.approve(mail.id)
    s.approvals_ids = (cal.id, mail.id)
    return s


def make(store, **kw):
    """One bundle, built the way the engine builds one.

    The display names the guests the update would email, and it is not decoration: a bundle that
    notifies cannot be approved without it (`ChangeSetStore.approve`), because the agreement
    recorded after execution is taken from what the bundle says was reviewed — a notifying bundle
    that names nobody leaves the guest list to be filled in from the provider's read-back.
    `calendar_display` stays overridable so a test can ask what happens to a bundle that does not.
    """
    cal_id, mail_id = store.approvals_ids
    kw.setdefault("calendar_display", _calendar_display(CAL_ARGS, attendees=("alex@example.com",)))
    return store.create(change_id="c1", task_id="task1", event_etag="etag-1",
                        boundary_id="pc1", boundary_version=1, now=at("09:00"),
                        calendar_args=CAL_ARGS, email_args=MAIL_ARGS,
                        approval_ids=(cal_id, mail_id), **kw)


def test_create_binds_every_action_to_its_exact_arguments(store):
    cs = make(store)
    assert cs.version == 1
    assert [a.kind for a in cs.actions()] == ["calendar_update", "email_reply"]
    assert cs.calendar.arguments == CAL_ARGS
    assert cs.email.arguments == MAIL_ARGS
    assert cs.calendar.approval_id != cs.email.approval_id


def test_approving_the_current_version_marks_both_actions_approved(store):
    cs = make(store)
    approved = store.approve(cs.id, 1, current_event_etag="etag-1", current_boundary_version=1,
                             now=at("09:05"))
    assert approved.status == "approved"
    for action in approved.actions():
        assert store.approvals.get(action.approval_id).status == APPROVED


def test_an_edit_is_a_new_version_and_kills_the_old_approvals(store):
    cs = make(store)
    store.approve(cs.id, 1, current_event_etag="etag-1", current_boundary_version=1,
                  now=at("09:05"))
    edited_args = {**MAIL_ARGS, "body": "Actually, 15:30."}
    new_mail = store.approvals.request("task1", "email_reply", edited_args)
    store.approvals.approve(new_mail.id)
    v2 = store.edit(cs.id, email_args=edited_args, approval_ids=(cs.calendar.approval_id,
                                                               new_mail.id), now=at("09:06"))
    assert v2.version == 2
    assert store.get(cs.id).version == 2
    # The superseded action's approval is dead, not merely unreferenced.
    assert store.approvals.get(cs.email.approval_id).status != APPROVED


def test_approving_a_superseded_version_is_409(store):
    cs = make(store)
    new_mail = store.approvals.request("task1", "email_reply", {**MAIL_ARGS, "body": "x"})
    store.approvals.approve(new_mail.id)
    store.edit(cs.id, email_args={**MAIL_ARGS, "body": "x"},
               approval_ids=(cs.calendar.approval_id, new_mail.id), now=at("09:06"))
    with pytest.raises(StaleChangeSet) as exc:
        store.approve(cs.id, 1, current_event_etag="etag-1", current_boundary_version=1,
                      now=at("09:07"))
    assert "version" in str(exc.value)


def test_a_changed_event_etag_is_409(store):
    cs = make(store)
    with pytest.raises(StaleChangeSet) as exc:
        store.approve(cs.id, 1, current_event_etag="etag-2", current_boundary_version=1,
                      now=at("09:05"))
    assert "changed" in str(exc.value)


def test_an_edited_private_constraint_is_409_even_though_the_event_did_not_move(store):
    cs = make(store)
    with pytest.raises(StaleChangeSet) as exc:
        store.approve(cs.id, 1, current_event_etag="etag-1", current_boundary_version=2,
                      now=at("09:05"))
    assert "protected" in str(exc.value)


def test_an_expired_bundle_is_409(store):
    cs = make(store)
    assert cs.expires_at > at("09:00")
    with pytest.raises(StaleChangeSet) as exc:
        store.approve(cs.id, 1, current_event_etag="etag-1", current_boundary_version=1,
                      now="2026-09-20T09:00:00+01:00")
    assert "expire" in str(exc.value)


def test_the_bundle_exposes_the_action_shape_the_ui_builds_against(store):
    cs = make(store)
    actions = {a["kind"]: a for a in cs.as_dict()["actions"]}
    assert set(actions) == {"calendar_update", "email_reply"}
    assert actions["calendar_update"]["event_id"] == "ev1"
    assert actions["calendar_update"]["notifies_attendees"] is True
    assert actions["email_reply"]["to"] == ["alex@example.com"]


def test_an_approval_cannot_be_spent_on_different_arguments(store):
    """Defence in depth under the version check: approval.py's fingerprint is the last word."""
    cs = make(store)
    store.approve(cs.id, 1, current_event_etag="etag-1", current_boundary_version=1,
                  now=at("09:05"))
    with pytest.raises(ApprovalError):
        store.approvals.consume(cs.email.approval_id, task_id="task1", tool="email_reply",
                                arguments={**MAIL_ARGS, "body": "something else"})
