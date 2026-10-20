#!/usr/bin/env python
"""Sanitize a live-run change-set (`prepare.json`) so it is safe to commit.

A live run's `prepare.json` is the engine's own record of one change: the inbound email, the
proposal it built, and the actions it would take. It is evidence a judge can check the flow
against — but as captured it carries a real sender address, real Gmail/Calendar ids, and
whatever query strings a search evidence URL happened to have. This strips exactly that, and
nothing else: the proposal, the reply body, the venue search results and the decision fields
are kept verbatim, because the point is to show what the engine did.

    python scripts/sanitize_live_run.py <source.json> <dest.json> [--forbidden VALUE ...]

Deterministic: the same input and the same `--forbidden` list always produce the same output,
so a judge holding the private source can re-run this and diff it against what is committed.
`--forbidden` is how a known secret-shaped value (a protected fact a run happened to echo back)
gets scrubbed too; it takes values on the command line, never a literal baked into this file —
this script must never be the place a secret it was written to remove gets committed.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
from collections.abc import Iterable

REDACTED = "<redacted>"

#: Addresses on these domains are fixture data (the corpus's own examples), not a real account.
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.net", "example.test"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: Keys whose value is a Gmail/Calendar/change-set id — real-looking, and enough on its own to
#: pull up the live record in a provider's console.
ID_KEYS = {"change_set_id", "event_id", "message_id", "thread_id"}

#: Every `http(s)://` run inside a string, wherever that string came from — a scalar URL field, or
#: a Markdown image or link embedded in a venue-search excerpt. A search URL carries its session
#: and tracking parameters in the query, and the query is the one part nobody needs in order to
#: check what the engine was shown.
URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]{}]+")

#: Characters that close a sentence or a Markdown link rather than belonging to the URL. They are
#: put back after the query is cut, so stripping a query never edits the prose around it.
URL_TRAILING_PUNCTUATION = ".,;:!?"


def _collect_emails(obj: object) -> list[str]:
    found: list[str] = []
    if isinstance(obj, str):
        found.extend(EMAIL_RE.findall(obj))
    elif isinstance(obj, dict):
        for value in obj.values():
            found.extend(_collect_emails(value))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(_collect_emails(value))
    return found


def _build_email_map(doc: dict) -> dict[str, str]:
    """Assign a role placeholder to every real-looking email address in the document.

    `from_address` becomes `sender@example.test` — a reply targets the same address, so this
    one mapping also covers every `to`/`cc` occurrence of it. Anything else not already on an
    allowed fixture domain gets a stable placeholder in order of first appearance, so this
    script never has to name a real address to know what to call it.
    """
    mapping: dict[str, str] = {}
    sender = doc.get("from_address") if isinstance(doc, dict) else None
    if isinstance(sender, str) and sender:
        domain = sender.rsplit("@", 1)[-1].lower()
        if domain not in ALLOWED_EMAIL_DOMAINS:
            mapping[sender.lower()] = "sender@example.test"

    next_n = 2
    for addr in _collect_emails(doc):
        low = addr.lower()
        domain = low.rsplit("@", 1)[-1]
        if domain in ALLOWED_EMAIL_DOMAINS or low in mapping:
            continue
        mapping[low] = f"person{next_n}@example.test"
        next_n += 1
    return mapping


def _redact_emails(obj: object, email_map: dict[str, str]) -> object:
    if isinstance(obj, str):
        def repl(match: re.Match[str]) -> str:
            addr = match.group(0)
            domain = addr.rsplit("@", 1)[-1].lower()
            if domain in ALLOWED_EMAIL_DOMAINS:
                return addr
            return email_map.get(addr.lower(), "redacted@example.test")
        return EMAIL_RE.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: _redact_emails(v, email_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_emails(v, email_map) for v in obj]
    return obj


def _is_calendar_entry(obj: dict) -> bool:
    """A `before`/`after` calendar snapshot: it names a real event id, next to a title/window."""
    return {"id", "title", "start", "end"} <= obj.keys()


def _redact_ids(obj: object) -> object:
    if isinstance(obj, dict):
        calendar_entry = _is_calendar_entry(obj)
        out = {}
        for k, v in obj.items():
            if k in ID_KEYS or (k == "id" and calendar_entry):
                out[k] = REDACTED
            else:
                out[k] = _redact_ids(v)
        return out
    if isinstance(obj, list):
        return [_redact_ids(v) for v in obj]
    return obj


def _strip_query_from_url(match: re.Match[str]) -> str:
    url = match.group(0)
    tail = ""
    while url and url[-1] in URL_TRAILING_PUNCTUATION:
        tail = url[-1] + tail
        url = url[:-1]
    if "?" in url:
        url = url.split("?", 1)[0]
    return url + tail


def _strip_url_queries(obj: object) -> object:
    if isinstance(obj, str):
        # Over the text, not over the scalar: a URL that reached the record inside an evidence
        # excerpt's Markdown is still a URL, and its query still has to go.
        return URL_RE.sub(_strip_query_from_url, obj)
    if isinstance(obj, dict):
        return {k: _strip_url_queries(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_url_queries(v) for v in obj]
    return obj


def sanitize_document(doc: dict, forbidden_values: Iterable[str] = ()) -> dict:
    """Sanitize one change-set record. Pure: the same `doc` always yields the same result."""
    email_map = _build_email_map(doc)
    out = _redact_emails(doc, email_map)
    out = _redact_ids(out)
    out = _strip_url_queries(out)
    if isinstance(out, dict) and "stage" in out and "id" in out:
        # The record's own id (the "change" a run's notes name) — not caught by
        # `_redact_ids`, which only matches an `id` inside a calendar-entry-shaped dict.
        out["id"] = REDACTED

    text = json.dumps(out)
    for value in forbidden_values:
        if value:
            text = text.replace(value, REDACTED)
    return json.loads(text)


def find_unredacted(doc: dict) -> list[str]:
    """Walk `doc` the same way `sanitize_document` does and report what it would have redacted.

    Empty means clean. This is the structural half of verifying a committed artifact: unlike a
    flat regex over the serialized text, it never flags a real-looking fragment sitting inside
    third-party scraped content (an evidence excerpt's image UUID, say) — only the specific
    id-shaped keys and the address fields sanitization actually touches.
    """
    problems: list[str] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            calendar_entry = _is_calendar_entry(obj)
            for k, v in obj.items():
                if (k in ID_KEYS or (k == "id" and calendar_entry)) and v != REDACTED:
                    problems.append(f"{k}={v!r} is not redacted")
                else:
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif isinstance(obj, str):
            for addr in EMAIL_RE.findall(obj):
                domain = addr.rsplit("@", 1)[-1].lower()
                if domain not in ALLOWED_EMAIL_DOMAINS:
                    problems.append(f"email {addr!r} is not on an allowed domain")

    walk(doc)
    if isinstance(doc, dict) and "stage" in doc and doc.get("id") != REDACTED:
        problems.append(f"top-level id={doc.get('id')!r} is not redacted")
    return problems


def sanitize_file(src: pathlib.Path, dst: pathlib.Path, forbidden_values: Iterable[str] = ()) -> None:
    doc = json.loads(src.read_text(encoding="utf-8"))
    sanitized = sanitize_document(doc, forbidden_values)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        json.dumps(sanitized, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=pathlib.Path, help="the private prepare.json to sanitize")
    parser.add_argument("dest", type=pathlib.Path, help="where to write the sanitized copy")
    parser.add_argument(
        "--forbidden", action="append", default=[], metavar="VALUE",
        help="a literal value to scrub wherever it appears (repeatable)",
    )
    args = parser.parse_args(argv)
    sanitize_file(args.source, args.dest, args.forbidden)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
