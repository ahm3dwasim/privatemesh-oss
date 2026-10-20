"""The live run's reply threaded by the wrong subject and carried no reply headers.

What the real run produced, on real Gmail:

* the subject was `Re: Could we meet near King's Cross tomorrow at 3 or 4?` — the REQUEST EXCERPT,
  not the thread's subject. The cause is structural rather than a bad draft: the draft phase was
  asked for a "subject", and the original subject was never in the payload it was given. The model
  had no way to answer correctly, so it prefixed the only thing it had.
* the raw MIME had no `In-Reply-To` and no `References`. Gmail still threaded the reply, but only
  because `threadId` was set — a header-less reply is threaded by the one provider that was told
  out of band, and by nothing else.

A reply subject is not a creative choice: it is `Re: <the thread's subject>`. So it is DERIVED
here, from the thread, by the code — and the model is no longer asked for it at all. The header
must carry the RFC 5322 `Message-ID` of the message being answered, not the provider's opaque id.
"""
from __future__ import annotations

import dataclasses

from fakes_v2 import Harness, change_email, evidence

THREAD_SUBJECT = "Café near King's Cross on Thursday"


def script(**over):
    base = {
        "classify": {"is_meeting_change": True},
        "extract": {"options": [{"start": "2026-09-19T15:00:00+01:00",
                                 "end": "2026-09-19T15:45:00+01:00", "phrase": "3"}],
                    "request_excerpt": "Could we meet near King's Cross tomorrow at 3 or 4?"},
        "query": {"query": "cafe near King's Cross opening hours", "include_domains": []},
        "choose": {"venue_index": 0, "reason_sentence": "It is open at 15:00.", "rejected": [],
                   "is_venue": True},
        # The live model's exact mistake: it invented the subject out of the request excerpt it was
        # handed. The engine must not listen to this field at all.
        "draft": {"subject": "Re: Could we meet near King's Cross tomorrow at 3 or 4?",
                  "template_body": "Hi Alex,",
                  "fragments": [{"source": "time", "text": "15:00-15:45 works for me."}]},
    }
    base.update(over)
    return base


def thread(*, answered_subject: str, root_subject: str = THREAD_SUBJECT):
    """The thread as it really arrives: a root message and the reply being handled."""
    root = change_email("m1", subject=root_subject)
    answered = change_email("m2", subject=answered_subject)
    return root, answered


def harness(*, answered_subject: str = THREAD_SUBJECT, root_subject: str = THREAD_SUBJECT,
            **kw):
    root, answered = thread(answered_subject=answered_subject, root_subject=root_subject)
    kw.setdefault("search", {"king's cross": [
        evidence("https://thestandard.co.uk/", "The Standard, King's Cross",
                 "Open daily 12:00-23:00", official=True)]})
    return Harness(messages=[answered], threads={"t1": [root, answered]},
                   script=script(**kw.pop("script", {})), **kw)


def prepared(h):
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready"
    return change, h.changesets.for_change(change.id).email.arguments


# ------------------------------------------------------------------ the subject ---------------
def test_the_reply_subject_is_the_threads_subject_not_the_request_excerpt():
    """The measured defect: the outgoing subject was `Re: <the request excerpt>`."""
    _, email = prepared(harness())
    assert email["subject"] == f"Re: {THREAD_SUBJECT}"


def test_the_draft_the_model_wrote_is_not_what_addresses_the_reply():
    """The model's own `subject` is discarded outright, whatever it says.

    This is the assertion that makes the fix structural: the draft above returns the live run's
    wrong subject verbatim, and the outgoing subject is right anyway."""
    h = harness()
    _, email = prepared(h)
    asked = h.rec.text("model")
    assert "Could we meet near King's Cross tomorrow at 3 or 4?" in asked      # it was really asked
    assert email["subject"] == f"Re: {THREAD_SUBJECT}"


def test_an_existing_reply_prefix_is_not_doubled():
    """The thread already carries the prefix, because the sender's mail client added it."""
    _, email = prepared(harness(answered_subject=f"Re: {THREAD_SUBJECT}"))
    assert email["subject"] == f"Re: {THREAD_SUBJECT}"


def test_the_re_spacing_variant_real_mail_had_is_stripped():
    """Real mail had `Re : ` with a space before the colon. It is still a reply prefix."""
    _, email = prepared(harness(answered_subject=f"Re : {THREAD_SUBJECT}"))
    assert email["subject"] == f"Re: {THREAD_SUBJECT}"


def test_a_reply_chain_is_collapsed_to_one_prefix():
    _, email = prepared(harness(answered_subject=f"RE: Re : re: {THREAD_SUBJECT}"))
    assert email["subject"] == f"Re: {THREAD_SUBJECT}"


def test_the_subject_comes_from_the_answered_message_not_the_thread_root():
    """A thread whose root was renamed: the message being answered is the one that is answered."""
    _, email = prepared(harness(answered_subject="Re: the renamed one",
                                root_subject="the original name"))
    assert email["subject"] == "Re: the renamed one"


def test_a_thread_with_no_subject_at_all_does_not_produce_a_subject_of_nothing():
    _, email = prepared(harness(answered_subject="", root_subject=""))
    assert email["subject"] == "Re:"
    assert not email["subject"].endswith("None")


# ------------------------------------------------------------------ the headers ---------------
def test_the_reply_names_the_rfc5322_message_id_of_the_message_it_answers():
    """`In-Reply-To` has to be the provider's `Message-ID` header — the RFC 5322 one.

    The Gmail id (`m2`) is opaque and means nothing outside Gmail, so a reply that carries it
    threads in exactly one client. The value has to be the bracketed Message-ID."""
    change, email = prepared(harness())
    assert email["rfc822_message_id"] == "<m2@example.com>"
    assert email["rfc822_message_id"] != change.message_id
    assert email["in_reply_to"] == change.message_id


def test_the_headers_are_taken_from_the_answered_message_and_not_the_newest_in_the_thread():
    """A thread that grew after the email was read must not retarget the reply."""
    root, answered = thread(answered_subject=THREAD_SUBJECT)
    later = change_email("m3", subject=THREAD_SUBJECT)
    h = Harness(messages=[answered], threads={"t1": [root, answered, later]},
                script=script(),
                search={"king's cross": [
                    evidence("https://thestandard.co.uk/", "The Standard, King's Cross",
                             "Open daily 12:00-23:00", official=True)]})
    _, email = prepared(h)
    assert email["rfc822_message_id"] == "<m2@example.com>"


def test_a_message_with_no_message_id_header_falls_back_without_inventing_one():
    """Some mail genuinely arrives without one. An empty header is the honest answer."""
    root = dataclasses.replace(change_email("m1", subject=THREAD_SUBJECT), rfc822_message_id="")
    answered = dataclasses.replace(change_email("m2", subject=THREAD_SUBJECT),
                                   rfc822_message_id="")
    h = Harness(messages=[answered], threads={"t1": [root, answered]}, script=script(),
                search={"king's cross": [
                    evidence("https://thestandard.co.uk/", "The Standard, King's Cross",
                             "Open daily 12:00-23:00", official=True)]})
    _, email = prepared(h)
    assert email["rfc822_message_id"] == ""
    assert email["subject"] == f"Re: {THREAD_SUBJECT}"
