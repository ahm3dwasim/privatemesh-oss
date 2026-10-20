"""Gmail over the REST API with plain httpx. No Google client library, on purpose: the whole
surface this product needs is six calls, and a dependency that can silently change auth behaviour
is a worse trade than two hundred lines that cannot.

Three things here are load-bearing for the hero flow:

* **The watched scope is a label, not the inbox.** `list_new` reads only messages carrying the
  PrivateMesh label. A label filter is an application boundary, not a narrower OAuth grant —
  the distinction is stated in the direction doc and it is stated again here so nobody mistakes
  the filter for a permission.
* **The cursor is millisecond-precise.** Gmail's `after:` takes whole seconds, so the cursor
  stores the newest `internalDate` *and* the ids at that exact millisecond. Without the ids, two
  messages arriving in the same second would let the second one disappear forever.
* **A reply is idempotent.** `send_reply` looks for the key before it sends, and `find_sent`
  re-reads each search hit's header before believing it. An ambiguous timeout must reconcile;
  it must never become a duplicate email to a real person.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid
from typing import Iterable, Protocol

import httpx

from .ports import (MailMessage, ReauthRequired, SentMail, sender_identity,
                    strip_quoted_and_comments)

log = logging.getLogger(__name__)

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
DEFAULT_LABEL = "PrivateMesh"
IDEMPOTENCY_HEADER = "X-PrivateMesh-Key"
CURSOR_VERSION = 1
MAX_SEARCH_CANDIDATES = 10


class GmailError(RuntimeError):
    """Gmail refused the call; the message names the status and the provider's reason."""


class TokenProvider(Protocol):
    def access_token(self) -> str: ...


def _addresses(value: str) -> tuple[str, ...]:
    return tuple(formataddr((name, addr)) if name else addr
                 for name, addr in getaddresses([value or ""]) if addr)


def build_reply(*, in_reply_to: MailMessage, to: Iterable[str], subject: str, body_text: str,
                idempotency_key: str) -> EmailMessage:
    """The message this product sends, built once, for every transport that sends it.

    Shared rather than duplicated because the review's Medium finding is precisely that the
    header set is a thing two places can disagree about: the live connector built the real MIME
    while the sample recorded a hand-written dict of seven fields, so the red-team gate was
    checking a list somebody maintained rather than the message that goes out. One builder means
    the sample's record of "what left" IS the live path's message, header for header.

    `From` is deliberately absent: Gmail assigns it from the authenticated account, and inventing
    a sender here would be this product claiming an identity it does not hold. That absence is
    what `reply_header_names()` reports, so a connector that ADDS one is visible."""
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    if in_reply_to.rfc822_message_id:
        message["In-Reply-To"] = in_reply_to.rfc822_message_id
        references = reply_references(in_reply_to)
        if references:
            message["References"] = " ".join(references)
    message["Message-ID"] = make_msgid(domain="privatemesh.local")
    message[IDEMPOTENCY_HEADER] = idempotency_key
    message.set_content(body_text)
    return message


def reply_header_names() -> tuple[str, ...]:
    """Every header name the send path sets — read off a message it actually built.

    Derived, not declared. A hand-maintained tuple of header names is the same defect as a
    hand-maintained list of fields to check: it is correct until somebody adds a header, and the
    red-team gate would keep reporting `held` for a message carrying one nobody listed. Building
    a probe through `build_reply` and reading its headers back means the gate's expectation moves
    with the code."""
    probe_parent = MailMessage(id="probe", thread_id="probe", sender="", to=(), subject="",
                               body_text="", received_at="1970-01-01T00:00:00Z",
                               rfc822_message_id="<probe@privatemesh.local>")
    probe = build_reply(in_reply_to=probe_parent, to=("probe@example.com",), subject="probe",
                        body_text="probe", idempotency_key="probe")
    return tuple(name for name, _value in probe.items())


def assert_approved_headers(message: EmailMessage) -> None:
    """Refuse a message whose header set the engine did not approve.

    Called on the FINAL message, immediately before it is serialised — not on the builder's output,
    because the finding is about what an ADAPTER adds. A provider adapter that sets a header after
    `build_reply` returns is adding outbound bytes nobody reviewed, and a check placed at the
    builder would run before that and report the message clean.

    The test is containment and not equality: a digest legitimately carries no `In-Reply-To`, so a
    missing approved header is fine and an extra one is not. That asymmetry is the whole rule —
    the message may say LESS than the schema allows and may never say more."""
    extra = {name for name, _value in message.items()} - APPROVED_HEADERS
    if extra:
        raise GmailError(
            f"refused: the message carries header(s) this build did not approve "
            f"({', '.join(sorted(extra))}). Every header is a field the recipient reads and a "
            f"header set is closed, so one that no part of this product set is a field nobody "
            f"reviewed.")


def _header(headers: Iterable[dict], name: str) -> str:
    wanted = name.lower()
    for h in headers or ():
        if str(h.get("name", "")).lower() == wanted:
            return str(h.get("value", ""))
    return ""


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def _body_text(payload: dict) -> str:
    """Depth-first for text/plain; Gmail nests it differently for every mail client."""
    leaves = _text_leaves(payload)
    plain = [text for mime, text in leaves if mime == "text/plain"]
    if plain:
        return plain[0]
    # No plain part: take any non-HTML sibling (a bare body with an odd mimeType), else nothing.
    # HTML is never returned — the caller falls back to the snippet rather than feeding markup
    # into a model or an email body.
    return next((text for mime, text in leaves if mime != "text/html"), "")


def _text_leaves(payload: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    data = (payload.get("body") or {}).get("data")
    if data:
        out.append((str(payload.get("mimeType") or ""), _decode(data)))
    for part in payload.get("parts") or ():
        out.extend(_text_leaves(part))
    return out


#: The `authserv-id` Gmail stamps on the `Authentication-Results` header its own receiving servers
#: add. A header with any other id was written by somebody else — quite possibly the sender.
GMAIL_AUTHSERV_ID = "mx.google.com"

# `strip_quoted_and_comments` moved to `ports` when `sender_identity` needed the same scanner for
# the `From` header itself; it is re-exported under this file's name so the reader below, and any
# other caller here, is unchanged. One scanner, because "what is syntax in this header" is exactly
# the question two readers must not answer differently.
_strip_quoted_and_comments = strip_quoted_and_comments


def _clause_authenticates(clause: str, domain: str) -> bool:
    """One `resinfo` clause: `method=result` FIRST, then `ptype.property=value` pairs.

    True only when the clause's own method is dkim or dmarc with the result exactly `pass`, and the
    domain that method vouches for (dkim: `header.d`, else the domain of `header.i`; dmarc:
    `header.from`) equals `domain`. Every value the clause gives for that property must equal it."""
    tokens = clause.split()
    if not tokens or "=" not in tokens[0]:
        return False
    method, _, result = tokens[0].partition("=")
    method, result = method.lower(), result.lower()
    if result != "pass" or method not in ("dkim", "dmarc"):
        return False
    props: dict[str, list[str]] = {}
    for token in tokens[1:]:
        name, eq, value = token.partition("=")
        if eq and value:
            props.setdefault(name.lower(), []).append(value)
    if method == "dmarc":
        values = props.get("header.from", [])
    else:
        values = props.get("header.d") or [v.rpartition("@")[2] for v in props.get("header.i", [])
                                           if "@" in v]
    return bool(values) and all(v.rstrip(".").lower() == domain for v in values)


def sender_authenticated(headers: Iterable[dict], from_value: str) -> bool:
    """Did GMAIL verify that the `From` address's own domain sent this message?

    What this reads, and what it trusts:

    * Only the FIRST `Authentication-Results` header in the message, and only when its authserv-id
      is Gmail's own (`mx.google.com`). Gmail prepends its verdict as it receives the message, so
      the first such header is Gmail's; one a sender wrote into its own message sits below it and is
      never read. A message with no Gmail verdict on top is not authenticated.
    * `dkim=pass` whose signing domain (`header.d`, or the domain of `header.i`) EQUALS the From
      domain, or `dmarc=pass` whose `header.from` EQUALS it. Full-domain equality, case-folded —
      never a label, prefix, suffix or substring, so `wentworth.attacker.example` passing DKIM for
      itself proves nothing about `wentworth.example.com`.
    * `spf=pass` alone is NOT accepted: SPF checks the envelope sender, not the `From` header the
      binding is about.

    What it does not prove: that the MAILBOX is the one the owner listed (DKIM signs for a domain,
    so another mailbox at the same domain also passes — the exact-address match is the caller's),
    or anything at all on a transport that is not Gmail."""
    address = sender_identity(from_value)
    if "@" not in address:
        return False
    domain = address.rsplit("@", 1)[1]
    first = next((str(h.get("value", "")) for h in headers or ()
                  if str(h.get("name", "")).lower() == "authentication-results"), "")
    authserv, _, results = _strip_quoted_and_comments(first).partition(";")
    if (authserv.split() or [""])[0].lower() != GMAIL_AUTHSERV_ID:
        return False
    return any(_clause_authenticates(clause, domain) for clause in results.split(";"))


def _parse_message(raw: dict) -> MailMessage:
    payload = raw.get("payload") or {}
    headers = payload.get("headers") or []
    internal_ms = int(raw.get("internalDate") or 0)
    received = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc)
    return MailMessage(
        id=str(raw.get("id") or ""),
        thread_id=str(raw.get("threadId") or ""),
        sender=_header(headers, "From"),
        to=_addresses(_header(headers, "To")),
        subject=_header(headers, "Subject"),
        body_text=_body_text(payload) or str(raw.get("snippet") or ""),
        # Milliseconds are kept: the cursor below is millisecond-precise, and a timestamp that
        # rounded them away could not resume the watch window exactly.
        received_at=received.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        rfc822_message_id=_header(headers, "Message-ID"),
        references=message_ids(_header(headers, "References")),
        sender_authenticated=sender_authenticated(headers, _header(headers, "From")),
    )


#: A message-id token in a header: angle-bracketed per RFC 5322, and every real mail client writes
#: it that way. The unbracketed fallback exists because a provider that hands back a bare id is
#: still telling us the parent's id, and dropping it would silently un-thread the reply.
_MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")


def message_ids(value: str) -> tuple[str, ...]:
    """The message-ids in a `References`/`Message-ID` header, in order, oldest first.

    Order is the whole point of the header, so this is a list-preserving parse and never a set.
    A repeated id is kept: RFC 5322 caps the chain at the first three entries of a long thread
    (and clients do the capping), but deciding which entries are "the same" is not this
    function's call — it reports what the header says."""
    text = str(value or "")
    found = _MESSAGE_ID_RE.findall(text)
    if found:
        return tuple(found)
    return tuple(tok for tok in text.split() if "@" in tok)


def reply_references(parent: MailMessage) -> tuple[str, ...]:
    """The `References` a reply to `parent` must carry: the parent's chain, then the parent.

    RFC 5322 §3.6.4, and the review's High finding. The previous code wrote the parent's id
    alone, which is correct for a one-message thread and wrong for every other one: replying to
    the third message of a thread threw away the two ids that tie it to the root, so a client
    reading headers rather than the provider's thread id files the reply as a new conversation.
    Gmail's `threadId` payload hides this — it threads correctly regardless — which is exactly
    why the defect survived a live run.

    The parent's own id is appended only when the chain does not already end with it. A provider
    that returns a message whose `References` already names it (some do, for the message being
    replied to) would otherwise get a duplicated entry."""
    chain = tuple(parent.references or ())
    own = str(parent.rfc822_message_id or "")
    if own and (not chain or chain[-1] != own):
        chain = chain + (own,)
    return chain


#: The header set this product may put on the wire, DERIVED from the one builder rather than
#: written out. It is a closed set in the direction that matters: a message carrying a header that
#: is not on it is refused, so `cc`, `bcc` and `reply-to` are impossible BY CONSTRUCTION rather
#: than by absence. The sixth review's second finding is the difference between those two — the
#: protocol has no `cc` parameter today, and "no parameter writes one" is a fact about today's
#: callers, which is not a rule and does not fail a test. `From` is deliberately not here: Gmail
#: assigns it from the authenticated account and inventing one would be this product claiming an
#: identity it does not hold — the check runs on the message THIS build composes, so its absence
#: from this set is the same statement `reply_header_names` makes.
#:
#: This sits below `reply_references` because deriving it calls the builder, and the builder calls
#: every helper above it. A constant that had to be hoisted back to the top of the file to be read
#: early would be one whose value depended on where the reader was standing.
APPROVED_HEADERS: frozenset = frozenset(reply_header_names())


def encode_cursor(ms: int, ids: list[str]) -> str:
    return json.dumps({"v": CURSOR_VERSION, "ms": ms, "ids": ids}, separators=(",", ":"))


def decode_cursor(cursor: str | None) -> tuple[int | None, set[str]]:
    if not cursor:
        return None, set()
    try:
        data = json.loads(cursor)
        return int(data.get("ms") or 0) or None, set(data.get("ids") or ())
    except (ValueError, TypeError, AttributeError):
        # A cursor we cannot read is treated as "start from now" rather than re-reading history:
        # re-delivering an old email into a live routine is worse than missing one poll.
        log.warning("unreadable Gmail cursor discarded; restarting the watch window")
        return None, set()


class GmailClient:
    """Implements `ports.MailPort` for one account."""

    def __init__(self, account: str = "you", *, http: httpx.Client | None = None,
                 tokens: TokenProvider | None = None, label: str = DEFAULT_LABEL,
                 base_url: str = GMAIL_BASE):
        self.account = account
        self.label = label
        self._base = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=30)
        self._tokens = tokens
        self._label_id: str | None = None

    # -------------------------------------------------------------- transport
    def _auth_header(self) -> dict:
        if self._tokens is None:
            raise GmailError(f"no Google token provider for account '{self.account}'")
        return {"Authorization": f"Bearer {self._tokens.access_token()}"}

    def _call(self, method: str, path: str, *, params: dict | None = None,
              json_body: dict | None = None) -> dict:
        url = f"{self._base}{path}"
        headers = self._auth_header()
        response = self._send(method, url, headers=headers, params=params, json_body=json_body)
        if response.status_code == 401:
            # A token can be revoked between the expiry check and the call. One forced refresh,
            # then the truth: reconnect. Never a retry loop against a provider that said no.
            refresh = getattr(self._tokens, "refresh", None)
            if not callable(refresh):
                raise ReauthRequired(
                    f"Gmail rejected the token for '{self.account}' — reconnect Google")
            refresh()
            response = self._send(method, url, headers=self._auth_header(), params=params,
                                  json_body=json_body)
            if response.status_code == 401:
                raise ReauthRequired(
                    f"Gmail rejected the refreshed token for '{self.account}' — "
                    f"reconnect Google (Testing-mode tokens expire after 7 days)")
        if response.status_code >= 400:
            raise GmailError(f"Gmail {method} {path} returned {response.status_code}: "
                             f"{self._reason(response)}")
        try:
            return response.json()
        except ValueError as exc:
            raise GmailError(f"Gmail {method} {path} returned a non-JSON body") from exc

    def _send(self, method: str, url: str, *, headers: dict, params: dict | None,
              json_body: dict | None) -> httpx.Response:
        try:
            return self._http.request(method, url, headers=headers, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise GmailError(f"Gmail {method} {url} failed: {exc!r}") from exc

    @staticmethod
    def _reason(response: httpx.Response) -> str:
        try:
            error = response.json().get("error") or {}
        except ValueError:
            return (response.text or "")[:200]
        if isinstance(error, str):
            return error
        reasons = [str(e.get("reason") or "") for e in error.get("errors") or ()]
        return " ".join(x for x in [str(error.get("message") or "")] + reasons if x)[:300]

    # -------------------------------------------------------------- watched label
    def label_id(self) -> str:
        if self._label_id:
            return self._label_id
        data = self._call("GET", "/labels")
        for label in data.get("labels") or ():
            if str(label.get("name")) == self.label:
                self._label_id = str(label.get("id"))
                return self._label_id
        raise GmailError(
            f"this Gmail account has no label named '{self.label}' — create it and route the "
            f"meeting thread into it; PrivateMesh only ever reads that label")

    # -------------------------------------------------------------- MailPort
    def list_new(self, since_cursor: str | None) -> tuple[list[MailMessage], str]:
        self.label_id()  # prove the watched scope exists before claiming there is nothing new
        cursor_ms, seen_ids = decode_cursor(since_cursor)
        params = {"q": f"label:{self.label}", "maxResults": "50"}
        if cursor_ms is not None:
            # One second of slack, because `after:` is second-granular; the exact filter below
            # is what actually decides, so the slack cannot duplicate or drop a message.
            params["q"] += f" after:{max(0, cursor_ms // 1000 - 1)}"
        listed = self._call("GET", "/messages", params=params)

        messages: list[MailMessage] = []
        for stub in listed.get("messages") or ():
            raw = self._call("GET", f"/messages/{stub.get('id')}", params={"format": "full"})
            internal_ms = int(raw.get("internalDate") or 0)
            mid = str(raw.get("id") or "")
            if cursor_ms is not None:
                if internal_ms < cursor_ms:
                    continue
                if internal_ms == cursor_ms and mid in seen_ids:
                    continue
            messages.append(_parse_message(raw))
        messages.sort(key=lambda m: m.received_at)

        if not messages:
            # Nothing new: the cursor must not move, or a message that arrives with an older
            # internalDate than the last one we saw would be skipped.
            return [], since_cursor or encode_cursor(cursor_ms or 0, [])
        new_ms = max(_ms_of(m) for m in messages)
        return messages, encode_cursor(new_ms, [m.id for m in messages if _ms_of(m) == new_ms])

    def get_thread(self, thread_id: str) -> list[MailMessage]:
        data = self._call("GET", f"/threads/{thread_id}", params={"format": "full"})
        messages = [_parse_message(m) for m in data.get("messages") or ()]
        messages.sort(key=lambda m: m.received_at)
        return messages

    def send_reply(self, in_reply_to: MailMessage, to: tuple[str, ...], subject: str,
                   body_text: str, idempotency_key: str) -> SentMail:
        existing = self.find_sent(idempotency_key)
        if existing is not None:
            log.info("reply %s already sent; returning the original instead of resending",
                     idempotency_key)
            return existing

        message = build_reply(in_reply_to=in_reply_to, to=to, subject=subject,
                              body_text=body_text, idempotency_key=idempotency_key)
        # THE LAST THING BEFORE THE WIRE. Everything above this line could have set a header, and
        # this is the only moment at which "the header set is the one this product approved" is a
        # statement about the bytes about to leave rather than about a builder's intentions.
        assert_approved_headers(message)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        payload: dict = {"raw": raw}
        if in_reply_to.thread_id:
            payload["threadId"] = in_reply_to.thread_id      # keeps the reply in the thread
        sent = self._call("POST", "/messages/send", json_body=payload)

        sent_id = str(sent.get("id") or "")
        thread_id = str(sent.get("threadId") or in_reply_to.thread_id)
        # Read the message back for the Message-ID the provider actually stored: reporting the one
        # we composed, when the provider may have rewritten it, would be a small lie in the record.
        rfc822 = str(message["Message-ID"])
        try:
            back = self._call("GET", f"/messages/{sent_id}",
                              params={"format": "metadata",
                                      "metadataHeaders": "Message-ID"})
            rfc822 = _header((back.get("payload") or {}).get("headers") or [], "Message-ID") or rfc822
        except GmailError as exc:
            log.warning("could not read back sent message %s: %s", sent_id, exc)
        return SentMail(id=sent_id, thread_id=thread_id, rfc822_message_id=rfc822)

    def find_sent(self, idempotency_key: str) -> SentMail | None:
        if not idempotency_key:
            return None
        found = self._call("GET", "/messages",
                           params={"q": f"in:anywhere {idempotency_key}",
                                   "maxResults": str(MAX_SEARCH_CANDIDATES)})
        for stub in found.get("messages") or ():
            mid = str(stub.get("id") or "")
            try:
                raw = self._call("GET", f"/messages/{mid}",
                                 params={"format": "metadata",
                                         "metadataHeaders": f"Message-ID, {IDEMPOTENCY_HEADER}"})
            except GmailError:
                continue
            headers = (raw.get("payload") or {}).get("headers") or []
            # Search is fuzzy; the header is exact. Only an exact match counts as "already sent".
            if _header(headers, IDEMPOTENCY_HEADER) == idempotency_key:
                return SentMail(id=mid, thread_id=str(raw.get("threadId") or ""),
                                rfc822_message_id=_header(headers, "Message-ID"))
        return None


def _ms_of(message: MailMessage) -> int:
    return int(datetime.fromisoformat(message.received_at.replace("Z", "+00:00")).timestamp() * 1000)


GmailPort = GmailClient  # the name the lane brief uses for this implementation
