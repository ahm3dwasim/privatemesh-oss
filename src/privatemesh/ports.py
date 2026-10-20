"""Ports: the interfaces between the v2 engine ("Handle the change") and the outside world.

Connectors (Gmail, Google Calendar, Tavily) implement these; the engine depends only on them, and
tests use in-memory fakes. Anything that talks to a real provider lives behind a port so a
public sample can run the REAL engine against deterministic simulators.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.utils import getaddresses
from typing import Protocol


# ---------------------------------------------------------------- mail
@dataclass(frozen=True)
class MailMessage:
    id: str
    thread_id: str
    sender: str            # full address, e.g. "Alex Doe <alex@example.com>"
    to: tuple[str, ...]
    subject: str
    body_text: str
    received_at: str       # ISO-8601 UTC
    rfc822_message_id: str = ""   # for In-Reply-To / References threading
    #: The parent's own `References` chain, oldest first, as message-ids (`<...>` included).
    #: RFC 5322 §3.6.4: a reply's `References` is THIS list followed by this message's
    #: `Message-ID` — a reply that replaces the chain instead of extending it un-threads the
    #: conversation for every client that reads the header rather than the provider's thread id.
    references: tuple[str, ...] = ()
    #: True only when the RECEIVING provider itself verified that the `From` address's own domain
    #: sent this message (Gmail: its own topmost `Authentication-Results` says `dkim=pass` with
    #: `header.d` equal to the From domain, or `dmarc=pass` with `header.from` equal to it — see
    #: `gmail.sender_authenticated`). False is the default and means "not shown", never "forged":
    #: `sender` itself is a header the sender writes, so without this nothing about it is proven.
    #: A refill auto-runs only when this is True (`ChangeEngine._refill_trust`).
    sender_authenticated: bool = False


@dataclass(frozen=True)
class SentMail:
    id: str
    thread_id: str
    rfc822_message_id: str


class MailPort(Protocol):
    def list_new(self, since_cursor: str | None) -> tuple[list[MailMessage], str]:
        """Messages in the watched scope newer than the cursor, oldest first, and the new cursor."""
    def get_thread(self, thread_id: str) -> list[MailMessage]: ...
    def send_reply(self, in_reply_to: MailMessage, to: tuple[str, ...], subject: str,
                   body_text: str, idempotency_key: str) -> SentMail:
        """Send exactly this body. `idempotency_key` lets reconciliation find a send whose
        response was lost, so an ambiguous timeout is never blindly resent."""
    def find_sent(self, idempotency_key: str) -> SentMail | None: ...


# ---------------------------------------------------------------- header text
def strip_quoted_and_comments(value: str) -> str:
    """Drop RFC 8601 comments (nested parentheses) and quoted strings, each replaced by a space.

    Both can carry arbitrary text — a `reason="dkim=pass header.d=..."` or a `(dmarc=pass ...)` —
    that is not a result, so nothing inside either is ever read as one. A backslash escapes the next
    character; an unterminated quote or comment swallows the rest (fail closed).

    Defined here rather than beside either caller because two readers of a `From`-style header need
    it: `gmail.sender_authenticated` reads the `Authentication-Results` clause, and
    `sender_identity` counts the addresses in the header itself. A second copy of this scanner
    would be a second answer to "what counts as syntax", which is the class of bug `sender_identity`
    exists to close."""
    out: list[str] = []
    depth = 0
    quoted = False
    escaped = False
    for ch in value:
        if escaped:
            escaped = False
        elif ch == "\\" and (quoted or depth):
            escaped = True
        elif quoted:
            quoted = ch != '"'
            if not quoted:
                out.append(" ")
        elif ch == '"' and not depth:
            quoted = True
        elif ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            if not depth:
                out.append(" ")
        elif not depth:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- identity
#: Deliberately structural: one `@`, something either side, and a dot in the domain. Not RFC 5322 —
#: this asks "is this an address at all", which is the question a sender that came back empty or
#: as a display name is failing.
_ADDRESS_RE = re.compile(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+")

#: RFC 5322 §2.2.3: CRLF followed by whitespace is a FOLD, line wrapping rather than a separator.
#: A header value a provider handed over still wrapped is one logical value, so it is unfolded to a
#: single space before anything is parsed or counted.
_FOLD_RE = re.compile(r"\r?\n[ \t]+")

#: One `<...>` group. COUNTED, never searched: the question is how many mailboxes the header names,
#: and `re.search` answers "at least one" — which is the whole of the bug this counts for.
_ANGLE_GROUP_RE = re.compile(r"<[^<>]*>")


def sender_identity(value: str) -> str:
    """The bare, case-folded address in a `From`-style header, or `""` when there is not one.

    ONE function for the whole product, because the review's Medium finding is that identity was
    normalized in two places and only one of them validated. Ingestion accepted whatever
    `_split_sender` produced — including a display name with no address in it — and execution
    normalized separately with its own regex, so "the sender" meant two slightly different things
    at the two ends of the same change. Here it means one thing: ONE syntactic address, lowercased,
    or nothing. Nothing is never a match, so an unreadable sender refuses rather than allows.

    ONE, and not the first of several. Reading the FIRST `<...>` group made
    `Wentworth <repeat@a.example> <x@evil.example>` answer `repeat@a.example` — a plausible address
    taken out of a header that names two mailboxes, which is the one thing a `From` header cannot
    legitimately do. Every caller then treated "the sender" as settled: the reply's audience check
    normalized the raw header back to that same first address and passed it, and what reached the
    `To` line was the whole string, which a provider reads as the first mailbox plus an empty
    second one. A header that does not hold exactly one mailbox holds no sender at all, and that is
    the fail-closed answer: no change, no reply, and a refusal on the record.

    Counting the `<...>` GROUPS was not enough, and the second review is why. It answers for the
    bracket spelling of two mailboxes and says nothing about a header that writes one of them
    WITHOUT brackets — `Name <trusted@example.com>, attacker@evil.example` has exactly one group,
    so it passed the group count, the first group's text was a well-formed address, and the answer
    was `trusted@example.com`: a real address, taken out of a header the attacker wrote, that the
    change was then pinned to and the reply addressed from. So the question is asked of the mailboxes
    themselves, not of the brackets:

    * how many `<...>` groups — the oldest of the three, and what the first review asked for;
    * how many `@` sit outside the quoted and commented text — the mixed form above has two, in
      either order, and a comma-separated pair with no brackets at all does too. A quoted display
      name holding an `@`, like `"evil@evil.example" <trusted@example.com>`, is not a mailbox and is
      blanked before the count, so it does not make an unambiguous header look ambiguous;
    * what the standard library's own split returns — `email.utils.getaddresses`, exactly one entry
      required, so the answer rests on a parser rather than on this function's regexes agreeing with
      themselves.

    **On the last two being one guard written twice, stated rather than implied.** They are the same
    question — how many mailboxes — asked of the text and of the parser, and on CPython 3.12 they
    agree on every input this lane could construct: a fuzz over the spelling variants (bracketed,
    bare, quoted, commented, group, folded, and every separator tried here) found no header the
    parser reads as one address while two `@` remain outside the quotes. So the `@` count is DEPTH,
    not a second gate, and it is kept for the one reason depth is kept: `getaddresses` is lenient by
    design and its leniency is not a specified behaviour — it gained a `strict` mode in 3.13 — so the
    invariant the header must satisfy is stated here in the header's own terms rather than borrowed
    from whatever the installed parser happens to do with malformed input. A guard in this codebase
    is expected to be one a test can decide; this one no test can decide alone, and saying so is
    better than a comment claiming a case it does not have.

    Group syntax is refused outright — `Group: a@x, b@y;` names a LIST, and a list is not a
    mailbox even when it holds exactly one, which is the case `getaddresses` reads as a plain
    single address (`Group: <a@x>;` returns `a@x` and would otherwise be accepted). This one a test
    decides on its own, and it is the reason the refusal cannot be left to the parser: nothing else
    here would catch a group of one. A colon or a semicolon that survives the blanking of quoted
    strings is group syntax or it is not a header at all; either way it is not one correspondent.

    Unfolding happens first, so a header wrapped across lines is read as the one value it is — a
    provider hands over long `From` values folded, and a fold inside the display name is the
    ordinary case this keeps working. A CR or LF that is NOT a fold is refused rather than folded
    away, because a raw line break inside a header value is how a second header gets written into
    the first one's place.

    **Nothing but the mailbox.** With no angle group at all the WHOLE value must be the address:
    `a@b.com (a note)` and `a@b.com junk` are refused, as they were before, rather than read as
    `a@b.com` plus trailing text a lenient provider is free to make something of."""
    text = _FOLD_RE.sub(" ", str(value or "")).strip()
    if not text or "\r" in text or "\n" in text:
        return ""
    bare = strip_quoted_and_comments(text)
    if ":" in bare or ";" in bare:
        return ""
    if len(_ANGLE_GROUP_RE.findall(text)) > 1:
        return ""
    if bare.count("@") != 1:
        return ""
    entries = getaddresses([text])
    if len(entries) != 1:
        return ""
    address = str(entries[0][1] or "").strip()
    if not _ADDRESS_RE.fullmatch(address):
        return ""
    if not _ANGLE_GROUP_RE.search(text) and not _ADDRESS_RE.fullmatch(text):
        return ""
    return address.casefold()


# ---------------------------------------------------------------- calendar
@dataclass(frozen=True)
class CalendarEvent:
    id: str
    etag: str
    summary: str
    start: str             # ISO-8601 with offset
    end: str
    timezone: str
    location: str = ""
    attendees: tuple[str, ...] = ()
    organizer_is_self: bool = True
    recurring: bool = False
    #: The organizer's own address, when the provider says the organizer is the owner. It is the
    #: only honest source for it — this build stores no address for the owner's own mailbox — and it
    #: is what lets clause 3 say "the invite update tells you and the person who wrote, and nobody
    #: else" instead of refusing every meeting that has the owner on its guest list. Empty for a
    #: provider that does not say, and for an event somebody else organizes.
    organizer_address: str = ""


@dataclass(frozen=True)
class BusyInterval:
    start: str
    end: str


class EtagMismatch(RuntimeError):
    """The event changed since it was reviewed (HTTP 412). Stop and require a new review."""


class ReauthRequired(RuntimeError):
    """The provider token is expired/revoked (e.g. Google Testing-mode 7-day expiry)."""


class CalendarPort(Protocol):
    def get_event(self, event_id: str) -> CalendarEvent: ...
    def list_events(self, day_start: str, day_end: str) -> list[CalendarEvent]: ...
    def free_busy(self, start: str, end: str) -> list[BusyInterval]: ...
    def update_event(self, event_id: str, if_match_etag: str, *, start: str, end: str,
                     location: str, send_updates: str, timezone: str = "") -> CalendarEvent:
        """Conditional update (If-Match). Raises EtagMismatch on 412. Returns the read-back event.

        `timezone` is the zone the engine approved for this write, and it is an argument rather
        than something the adapter works out: it is the field that tells the provider how to READ
        `start`/`end`, so an adapter that chose it itself would be deciding, after the approval,
        which wall clock the owner's instant lands on. An adapter that is handed one must refuse to
        write it against an event whose own zone says otherwise — see `GCalClient.update_event`,
        where a mismatch raises rather than silently preferring either value."""


# ---------------------------------------------------------------- search
@dataclass(frozen=True)
class Evidence:
    url: str
    title: str
    excerpt: str           # the passage that supports (or rejects) a claim
    retrieved_at: str      # ISO-8601 UTC
    official: bool = False # source is the venue's own site / official listing


class SearchPort(Protocol):
    def search(self, query: str, *, max_results: int = 5,
               include_domains: tuple[str, ...] = ()) -> list[Evidence]: ...


# ---------------------------------------------------------------- the private boundary
@dataclass(frozen=True)
class ProtectedCommitment:
    """Owner-local. Only `start`, `end` and `buffer_minutes` may ever leave the device, and only
    as an anonymous planning fact ("busy, protected"). title/location/reason NEVER leave."""
    id: str
    start: str
    end: str
    buffer_minutes: int
    version: int
    title: str = ""
    location: str = ""
    reason: str = ""
    attendees: tuple[str, ...] = field(default_factory=tuple)
