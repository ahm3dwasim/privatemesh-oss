"""gmail: the MailPort over the Gmail REST API, against an httpx.MockTransport.

The three properties worth defending here are the ones the hero flow depends on: the watched label
is a real filter with a resumable cursor that cannot duplicate or drop a message, a reply is
threaded and carries the idempotency key, and a lost response is reconciled instead of resent.
"""
from __future__ import annotations

import base64
import json
import logging
from email import message_from_bytes
from email.header import decode_header, make_header
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from privatemesh import gmail as gm
from privatemesh.ports import ReauthRequired

LABEL_ID = "Label_42"
THREAD = "thr-1"


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def raw_message(mid: str, *, internal_ms: int, sender="Alex Doe <alex@example.com>",
                subject="Our usual café has closed", body="Could we meet near King's Cross "
                "tomorrow at 3 or 4? You choose and update the invite.",
                thread=THREAD, rfc822="<alex-1@example.com>", to="owner@example.com",
                labels=(LABEL_ID,), mime="text/plain", parts=None) -> dict:
    payload = {"mimeType": mime,
               "headers": [{"name": "From", "value": sender}, {"name": "To", "value": to},
                           {"name": "Subject", "value": subject},
                           {"name": "Message-ID", "value": rfc822},
                           {"name": "Date", "value": "Thu, 18 Sep 2026 09:40:00 +0000"}]}
    if parts is None:
        payload["body"] = {"data": b64(body)}
    else:
        payload["parts"] = parts
    return {"id": mid, "threadId": thread, "internalDate": str(internal_ms),
            "labelIds": list(labels), "snippet": body[:60], "payload": payload}


class FakeGmail:
    """Scripted Gmail. Records every request so the tests can assert on the wire, not the wrapper."""

    def __init__(self, messages=(), *, label_missing=False, send_401=False, send_401_then_ok=False,
                 search_hits=(), search_metadata=None, labels=None):
        self.messages = {m["id"]: m for m in messages}
        self.label_missing = label_missing
        self.send_401 = send_401
        self.send_401_then_ok = send_401_then_ok
        self._send_401s_left = 1 if send_401_then_ok else 0
        self.search_hits = list(search_hits)
        self.search_metadata = search_metadata or {}
        self.labels = labels or [{"id": LABEL_ID, "name": gm.DEFAULT_LABEL}]
        self.requests: list[httpx.Request] = []
        self.sent: list[dict] = []
        self._sent_n = 0
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    # -- helpers for assertions
    def calls(self, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(suffix)]

    def query_of(self, req: httpx.Request) -> str:
        return parse_qs(urlparse(str(req.url)).query).get("q", [""])[0]

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if path.endswith("/labels"):
            return httpx.Response(200, json={"labels": [] if self.label_missing else self.labels})
        if path.endswith("/messages/send"):
            return self._handle_send(req)
        if path.endswith("/messages"):
            q = self.query_of(req)
            if q.startswith("in:anywhere"):
                if self.search_hits:
                    return httpx.Response(200, json={"messages": [{"id": i}
                                                                  for i in self.search_hits]})
                # Real search is fuzzy; model it as "everything carrying that key text".
                hits = [s["id"] for s in self.sent if s["key"] and s["key"] in q]
                return httpx.Response(200, json={"messages": [{"id": i} for i in hits]})
            return httpx.Response(200, json={"messages": [{"id": i} for i in self._in_scope(q)]})
        if "/messages/" in path:
            mid = path.rsplit("/", 1)[-1]
            body = self.search_metadata.get(mid) or self.messages.get(mid)
            if body is None:
                return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
            return httpx.Response(200, json=body)
        if "/threads/" in path:
            tid = path.rsplit("/", 1)[-1]
            found = [m for m in self.messages.values() if m["threadId"] == tid]
            return httpx.Response(200, json={"id": tid, "messages": found})
        return httpx.Response(404, json={"error": {"code": 404, "message": "no route"}})

    def _in_scope(self, q: str) -> list[str]:
        if f"label:{gm.DEFAULT_LABEL}" not in q:
            return []
        after = int(q.split("after:", 1)[1].split()[0]) if "after:" in q else 0
        return sorted((m["id"] for m in self.messages.values()
                       if int(m["internalDate"]) // 1000 >= after),
                      key=lambda i: int(self.messages[i]["internalDate"]))

    def _handle_send(self, req: httpx.Request) -> httpx.Response:
        if self.send_401 or self._send_401s_left > 0:
            self._send_401s_left -= 1
            return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials",
                                                       "status": "UNAUTHENTICATED"}})
        payload = json.loads(req.content)
        self._sent_n += 1
        mid = f"sent-{self._sent_n}"
        mime = message_from_bytes(base64.urlsafe_b64decode(payload["raw"]))
        self.sent.append({"id": mid, "threadId": payload.get("threadId"),
                          "mime": mime, "raw": payload,
                          "key": mime.get(gm.IDEMPOTENCY_HEADER)})
        stored = raw_message(mid, internal_ms=1_700_000_000_000, sender="owner@example.com",
                             subject="re", body="ok", thread=payload.get("threadId") or THREAD,
                             rfc822=mime.get("Message-ID") or "<sent@example.com>",
                             to="alex@example.com")
        stored["payload"]["headers"].append({"name": gm.IDEMPOTENCY_HEADER,
                                             "value": mime.get(gm.IDEMPOTENCY_HEADER)})
        self.messages[mid] = stored
        return httpx.Response(200, json={"id": mid, "threadId": payload.get("threadId"),
                                         "labelIds": ["SENT"]})

    def metadata_for_search(self, mid: str, key: str | None) -> dict:
        headers = [{"name": "Message-ID", "value": "<sent-1@example.com>"}]
        if key is not None:
            headers.append({"name": gm.IDEMPOTENCY_HEADER, "value": key})
        return {"id": mid, "threadId": THREAD, "internalDate": "1700000000000",
                "payload": {"headers": headers}}


class Tokens:
    def __init__(self, value="ya29.TEST-TOKEN", *, refresh=None):
        self.value = value
        self.refresh = refresh
        self.calls = 0

    def access_token(self) -> str:
        self.calls += 1
        return self.value


def client(fake: FakeGmail, tokens=None, **kw) -> gm.GmailClient:
    return gm.GmailClient("you", http=fake.client, tokens=tokens or Tokens(), **kw)


# ------------------------------------------------------------------ list_new -------------------
def test_list_new_returns_the_watched_label_oldest_first_with_a_cursor():
    older = raw_message("m-1", internal_ms=1_700_000_000_000)
    newer = raw_message("m-2", internal_ms=1_700_000_060_000)
    fake = FakeGmail([newer, older])          # Gmail answers newest-first
    messages, cursor = client(fake).list_new(None)

    assert [m.id for m in messages] == ["m-1", "m-2"]
    assert messages[0].sender == "Alex Doe <alex@example.com>"
    assert messages[0].thread_id == THREAD
    assert messages[0].rfc822_message_id == "<alex-1@example.com>"
    assert messages[0].received_at.startswith("2023-11-14T")
    assert "King's Cross" in messages[0].body_text
    assert cursor and json.loads(cursor)["ms"] == 1_700_000_060_000


def test_list_new_asks_for_the_watched_label_only():
    fake = FakeGmail([raw_message("m-1", internal_ms=1_700_000_000_000)])
    client(fake).list_new(None)
    q = fake.query_of(fake.calls("/messages")[0])
    assert f"label:{gm.DEFAULT_LABEL}" in q


def test_a_second_poll_with_the_returned_cursor_yields_no_duplicates():
    fake = FakeGmail([raw_message("m-1", internal_ms=1_700_000_000_000),
                      raw_message("m-2", internal_ms=1_700_000_060_000)])
    g = client(fake)
    first, cursor = g.list_new(None)
    assert [m.id for m in first] == ["m-1", "m-2"]

    again, cursor2 = g.list_new(cursor)
    assert again == [], "the same two messages must not be reported twice"

    fake.messages["m-3"] = raw_message("m-3", internal_ms=1_700_000_120_000)
    third, _ = g.list_new(cursor2)
    assert [m.id for m in third] == ["m-3"]


def test_a_new_message_sharing_the_cursor_millisecond_is_still_delivered():
    """Second-granularity Gmail queries mean the boundary millisecond must be reopened by id."""
    fake = FakeGmail([raw_message("m-1", internal_ms=1_700_000_000_000)])
    g = client(fake)
    _, cursor = g.list_new(None)
    fake.messages["m-2"] = raw_message("m-2", internal_ms=1_700_000_000_000)  # same millisecond
    again, _ = g.list_new(cursor)
    assert [m.id for m in again] == ["m-2"]


def test_a_missing_watched_label_is_a_clear_setup_error():
    fake = FakeGmail([], label_missing=True)
    with pytest.raises(gm.GmailError) as err:
        client(fake).list_new(None)
    assert gm.DEFAULT_LABEL in str(err.value)


def test_get_thread_returns_the_thread_oldest_first():
    fake = FakeGmail([raw_message("m-2", internal_ms=1_700_000_060_000),
                      raw_message("m-1", internal_ms=1_700_000_000_000)])
    messages = client(fake).get_thread(THREAD)
    assert [m.id for m in messages] == ["m-1", "m-2"]


def test_a_multipart_body_reads_the_plain_text_part():
    parts = [{"mimeType": "text/html", "body": {"data": b64("<p>html</p>")}},
             {"mimeType": "text/plain", "body": {"data": b64("the plain text")}}]
    fake = FakeGmail([raw_message("m-1", internal_ms=1_700_000_000_000, mime="multipart/alternative",
                                  parts=parts)])
    messages, _ = client(fake).list_new(None)
    assert messages[0].body_text == "the plain text"


# ------------------------------------------------------------------ send_reply -----------------
def test_send_reply_is_threaded_and_carries_the_idempotency_key():
    fake = FakeGmail([raw_message("m-1", internal_ms=1_700_000_000_000)])
    g = client(fake)
    original, _ = g.list_new(None)
    sent = g.send_reply(original[0], to=("Alex Doe <alex@example.com>",),
                        subject="Re: Our usual café has closed",
                        body_text="15:00 works. Updated the invite.", idempotency_key="key-abc")

    body = fake.sent[0]
    mime = body["mime"]
    assert mime["In-Reply-To"] == "<alex-1@example.com>"
    assert mime["References"] == "<alex-1@example.com>"
    assert mime["To"] == "Alex Doe <alex@example.com>"
    # The wire format is RFC 2047 encoded; decoding it is the test that the accented café name
    # survives the round trip rather than arriving as mojibake at a real recipient.
    assert str(make_header(decode_header(mime["Subject"]))) == "Re: Our usual café has closed"
    assert mime["X-PrivateMesh-Key"] == "key-abc"
    assert mime.get_content_type() == "text/plain"
    assert "15:00 works" in mime.get_payload(decode=True).decode()
    assert body["threadId"] == THREAD, "a reply must stay in the original thread"
    assert sent.thread_id == THREAD
    assert sent.id == "sent-1"
    assert sent.rfc822_message_id == body["mime"]["Message-ID"]


def test_send_reply_with_the_same_key_is_not_sent_twice():
    fake = FakeGmail([raw_message("m-1", internal_ms=1_700_000_000_000)])
    g = client(fake)   # nothing sent yet: the first call really sends, the second must not
    original, _ = g.list_new(None)
    g.send_reply(original[0], to=("alex@example.com",), subject="Re: x", body_text="hi",
                 idempotency_key="key-abc")

    again = g.send_reply(original[0], to=("alex@example.com",), subject="Re: x", body_text="hi",
                         idempotency_key="key-abc")
    assert len(fake.sent) == 1, "an ambiguous timeout must reconcile, never resend"
    assert again.id == "sent-1"


def test_find_sent_verifies_the_header_instead_of_trusting_the_search_hit():
    fake = FakeGmail()
    fake.search_hits = ["m-9"]
    fake.search_metadata = {"m-9": fake.metadata_for_search("m-9", "some-other-key")}
    assert client(fake).find_sent("key-abc") is None

    fake.search_metadata = {"m-9": fake.metadata_for_search("m-9", "key-abc")}
    assert client(fake).find_sent("key-abc").id == "m-9"


def test_find_sent_returns_none_when_nothing_matches():
    fake = FakeGmail()
    assert client(fake).find_sent("key-abc") is None


# ------------------------------------------------------------------ auth / errors --------------
def test_a_401_is_refreshed_once_and_then_retried():
    fake = FakeGmail([raw_message("m-1", internal_ms=1_700_000_000_000)], send_401_then_ok=True)
    calls: list[int] = []
    tokens = Tokens(refresh=lambda: calls.append(1))
    g = gm.GmailClient("you", http=fake.client, tokens=tokens)
    original, _ = g.list_new(None)
    sent = g.send_reply(original[0], to=("alex@example.com",), subject="Re: x", body_text="hi",
                        idempotency_key="key-abc")
    assert sent.id == "sent-1"
    assert calls == [1], "exactly one refresh, then one retry"


def test_a_401_that_survives_the_refresh_requires_reconnecting():
    fake = FakeGmail(send_401=True)
    tokens = Tokens(refresh=lambda: None)
    g = gm.GmailClient("you", http=fake.client, tokens=tokens)
    with pytest.raises(ReauthRequired):
        g.send_reply(gm.MailMessage(id="m-1", thread_id=THREAD, sender="a@b", to=("c@d",),
                                    subject="s", body_text="b", received_at="2026-09-18T09:40:00Z",
                                    rfc822_message_id="<alex-1@example.com>"),
                     to=("alex@example.com",), subject="Re: x", body_text="hi",
                     idempotency_key="key-abc")


def test_a_provider_error_is_reported_with_its_status_and_reason():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": 403, "message": "Rate exceeded",
                                                   "errors": [{"reason": "rateLimitExceeded"}]}})

    g = gm.GmailClient("you", http=httpx.Client(transport=httpx.MockTransport(handler)),
                       tokens=Tokens())
    with pytest.raises(gm.GmailError) as err:
        g.list_new(None)
    assert "403" in str(err.value)
    assert "rateLimitExceeded" in str(err.value) or "Rate exceeded" in str(err.value)


def test_no_token_value_reaches_the_logs(caplog):
    fake = FakeGmail([raw_message("m-1", internal_ms=1_700_000_000_000)])
    g = client(fake, Tokens("ya29.SUPER-SECRET"))
    with caplog.at_level(logging.DEBUG):
        g.list_new(None)
    combined = caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    assert "SUPER-SECRET" not in combined
    for req in fake.requests:
        assert "SUPER-SECRET" not in str(req.url)
