"""Tavily as an evidence source, not a link list.

The direction doc is blunt about this: "A list of links added after an already-made choice is weak
bonus-prize positioning." So this wrapper keeps the two things a decision needs:

* **the passage**, not the page: `excerpt` is the densest window of the query's own terms, capped
  at 400 characters, and it says so with ellipses when it has been trimmed. A fact shown beside a
  claim has to be readable next to that claim.
* **whose site it is**: `official` is decided by the registrable domain against the venue's name,
  never by Tavily's ranking. An aggregator that ranks first is still an aggregator, and the
  proposal screen shows one rejected option with its reason — that screen is only honest if the
  flag it renders is about provenance.

The excerpt is deliberately mechanical. Where the public record is thin, the result is a short or
missing excerpt and the caller is expected to say "no adequate source" rather than to fill the gap.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

import httpx

from .ports import Evidence

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
EXCERPT_LIMIT = 400
MIN_TOKEN = 3

# Words that describe the *question* rather than the venue. They are dropped from the excerpt
# scoring keywords, and the subject of a query is everything before the first one.
STOPWORDS = frozenset("""
a an and are at be best book booking by can close closed closes for from has have how in is it
its menu near of on open opening opens or phone price prices reserve reservation review reviews
the time times to today tomorrow was weekday weekdays weekend what when where which who why with
""".split())

# Sites that talk *about* venues. A venue's own site is the only thing that can be "official" for
# an opening-hours claim; Wikipedia is not the café's word either.
AGGREGATORS = frozenset("""
yelp tripadvisor facebook instagram twitter x reddit wikipedia wikimedia google maps bing apple
foursquare zomato opentable thefork squaremeal dojo designmynight timeout timeoutout
deliveroo justeats ubereats booking hotels airbnb agoda expedia yellowpages lycos urbanspoon
""".split())

# Suffixes where the registrable label is the third from the right, not the second.
MULTI_PART_SUFFIXES = frozenset("""
co.uk org.uk ac.uk gov.uk me.uk net.uk co.nz net.nz org.nz com.au net.au org.au co.jp ne.jp
co.in com.br com.mx co.za com.sg com.hk co.il com.tr co.kr
""".split())


class TavilyError(RuntimeError):
    """Tavily refused the call. Never silently read as 'no results'."""


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


def _tokens(text: str) -> list[str]:
    """Content words, in order, for scoring and for naming the subject."""
    words = re.findall(r"[^\W_]+", (text or "").lower(), flags=re.UNICODE)
    return [w for w in words if len(w) >= MIN_TOKEN and w not in STOPWORDS]


def registrable_label(url: str) -> str:
    """The domain label a human would name: `grangerandco.com` -> `grangerandco`."""
    host = urlparse(url or "").netloc.lower().split(":")[0].strip(".")
    if not host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return ""
    if host.replace(".", "").isdigit():
        return ""                     # a bare IP address is nobody's official site
    labels = [part for part in host.split(".") if part]
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_PART_SUFFIXES:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else ""


def is_official(url: str, subject: str) -> bool:
    """True only when the page's own domain is the venue's name."""
    label = registrable_label(url)
    if not label or label in AGGREGATORS:
        return False
    slug, name = _slug(label), _slug(subject)
    if len(slug) < 4 or not name:
        return False
    if slug in name or name in slug:
        return True
    # `grangerandco` vs "Granger & Co": the words are in there, just squeezed together.
    return any(word in slug for word in _tokens(subject) if len(word) >= 4)


def subject_from_query(query: str) -> str:
    """The venue's name as far as the query states it: everything before the first question word,
    or an explicit "quoted phrase" when the caller supplies one."""
    quoted = re.search(r"[\"“”']([^\"“”']{2,60})[\"“”']", query or "")
    if quoted:
        return quoted.group(1).strip()
    kept: list[str] = []
    for raw in (query or "").split():
        word = re.sub(r"[^\w&'-]", "", raw)
        if not word or (word.lower() in STOPWORDS and kept):
            break
        if word.lower() in STOPWORDS and not kept:
            continue
        kept.append(word)
        if len(kept) == 4:
            break
    return " ".join(kept).strip()


def excerpt_around(text: str, query: str, limit: int = EXCERPT_LIMIT) -> str:
    """The `limit`-character window carrying the most of the query's own words."""
    body = " ".join((text or "").split())
    if not body:
        return ""
    if len(body) <= limit:
        return body

    keywords = _tokens(query)
    lowered = body.lower()
    starts: list[int] = []
    for word in keywords:
        at = lowered.find(word)
        while at != -1 and len(starts) < 400:
            starts.append(max(0, at - limit // 4))
            at = lowered.find(word, at + 1)
    if not starts:
        starts = [0]

    def score(start: int) -> int:
        window = lowered[start:start + limit]
        return sum(1 for word in keywords if word in window)

    best = max(starts, key=lambda start: (score(start), -start))
    start = best if score(best) else 0
    end = min(len(body), start + limit)
    truncated_front, truncated_back = start > 0, end < len(body)
    if truncated_front:                       # cut on a word boundary, not mid-word
        space = body.find(" ", start)
        start = space + 1 if 0 <= space < end else start
    if truncated_back:
        space = body.rfind(" ", start, end)
        end = space if space > start else end

    budget = limit - (1 if truncated_front else 0) - (1 if truncated_back else 0)
    piece = body[start:start + budget]
    return ("…" if truncated_front else "") + piece + ("…" if truncated_back else "")


class TavilySearch:
    """Implements `ports.SearchPort`."""

    def __init__(self, api_key: str, *, http: httpx.Client | None = None,
                 clock: Callable[[], str] | None = None, url: str = TAVILY_URL):
        self.api_key = api_key
        self._http = http or httpx.Client(timeout=30)
        self._clock = clock or _utc_now
        self._url = url

    def search(self, query: str, *, max_results: int = 5,
               include_domains: tuple[str, ...] = (), subject: str | None = None,
               ) -> list[Evidence]:
        """`subject` names the venue the question is about; the official flag is a judgement about
        the venue, so a caller that knows the name should pass it. Without it the name is read
        from the query, which is a guess."""
        if not self.api_key:
            raise TavilyError("TAVILY_API_KEY is not set (see .env.example)")
        body: dict = {"api_key": self.api_key, "query": query, "search_depth": "advanced",
                      "max_results": int(max_results), "include_raw_content": True}
        if include_domains:
            body["include_domains"] = list(include_domains)
        try:
            response = self._http.post(self._url, json=body,
                                       headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            raise TavilyError(f"Tavily search failed: {exc!r}") from exc
        if response.status_code >= 400:
            raise TavilyError(f"Tavily returned {response.status_code}: "
                              f"{(response.text or '')[:200]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TavilyError("Tavily returned a non-JSON body") from exc

        venue = subject or subject_from_query(query)
        retrieved_at = self._clock()
        evidence: list[Evidence] = []
        for item in payload.get("results") or ():
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            text = str(item.get("raw_content") or item.get("content") or "")
            if not (url and text.strip()):
                continue      # a result with no readable content is not evidence of anything
            evidence.append(Evidence(
                url=url,
                title=str(item.get("title") or ""),
                excerpt=excerpt_around(text, query),
                retrieved_at=retrieved_at,
                official=is_official(url, venue),
            ))
        return evidence


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
