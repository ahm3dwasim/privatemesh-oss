# Ported from the author's prior private assistant (Jarvis); adapted for PrivateMesh.
"""Secret hygiene: which paths/commands are sensitive, and a small value redactor.

Lifted (deliberately narrowly) from the prior assistant's 1,632-line guards module: the
path/command *classifiers* and a plain env-value scrubber. The decoder machinery that
module grew — base64 / percent-encoding / HTML-entity / unicode-escape projections with
bounded scans — is hardening for a production assistant holding real credentials, and is
NOT part of this port. What survives here is the ~100 lines that a demo actually needs:
know when a path or command is touching a secret, and never let a secret value reach a
model or the memory store in the clear.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Reading these is not "nothing leaves the machine" once an egress tool can carry them off-box.
SECRET_PATH_RE = re.compile(
    r"(\.env\b|\.pem\b|\.key\b|id_rsa|id_ed25519|\.ssh/|authorized_keys|\.aws\b|\.netrc|"
    r"\.credentials|/etc/shadow|private[_-]?key|\.p12\b|\.pfx\b|keychain|\.gnupg)",
    re.IGNORECASE,
)

# Writing these establishes persistence / autostart — more than "created, not delivered".
PERSISTENCE_PATH_RE = re.compile(
    r"(LaunchAgents|LaunchDaemons|/etc/|systemd|\.service\b|crontab|cron\.d|\.zshrc|\.zshenv|"
    r"\.zprofile|\.bashrc|\.bash_profile|\.profile|autostart|authorized_keys|sudoers)",
    re.IGNORECASE,
)

# A bare environment dump, or reading the process environ.
ENV_DUMP_RE = re.compile(
    r"(^|[;&|`]|\s)(env|printenv|set|export\s+-p|declare\s+-x)(\s|;|&|\||$)|/proc/(self|\d+)/environ",
    re.IGNORECASE,
)

# Env var NAMES that hold secrets (matches inside compound names like NEBIUS_API_KEY, which a
# `\b`-anchored regex misses because '_' is a word character).
SENSITIVE_ENV_KEY_RE = re.compile(
    r"(API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE[_-]?KEY)", re.IGNORECASE
)

# Secret-shaped CONTENT, for scrubbing free text that never came from os.environ.
SECRET_VALUE_RE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{12,}|sk-ant-[A-Za-z0-9_-]{8,}|"
    r"sk-[A-Za-z0-9_-]{20,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"AIza[0-9A-Za-z_\-]{20,}|tvly-[A-Za-z0-9_-]{16,}|nvapi-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9]{20,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)?)"
)

# `NAME=value` / `name: value` where NAME looks like a credential (a pasted .env line, a config
# snippet, "password: ..."). Only the VALUE is replaced, so the sentence stays readable.
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIALS?|"
    r"PRIVATE[_-]?KEY|ACCESS[_-]?KEY)[A-Za-z0-9_.-]*)(\s*[:=]\s*)([\"']?)([^\s\"',;]{6,})\3"
)

REDACTED = "[REDACTED]"
# A short env value is a common word far more often than it is a secret; scrubbing it would
# corrupt ordinary text (and memories) for no security gain.
_MIN_SECRET_LEN = 8
_MAX_SECRETS = 64


def _package_root() -> Path:
    """The PrivateMesh package directory — its own code and state, never a write target for a skill."""
    return Path(__file__).resolve().parent


def is_within_package(target: str) -> bool:
    """True if `target` resolves inside the PrivateMesh package (its own code and state)."""
    try:
        p = Path(target).expanduser().resolve()
        root = _package_root()
        return p == root or root in p.parents
    except Exception:
        return any(s in target for s in ("privatemesh/", "privatemesh\\"))


def path_is_sensitive(target: str) -> bool:
    """A write target that must be confirmed: secret, persistence, or inside the package.

    Fails safe: an unparseable target is treated as sensitive rather than waved through."""
    try:
        return bool(SECRET_PATH_RE.search(target) or PERSISTENCE_PATH_RE.search(target)) or is_within_package(target)
    except Exception:
        return True


def sensitive_env_names() -> list[str]:
    """Names of environment variables whose VALUE is a secret."""
    try:
        return [k for k in os.environ if SENSITIVE_ENV_KEY_RE.search(k)]
    except Exception:
        return []


def command_touches_secret(command: str) -> bool:
    """True if a shell command dumps the environment or references a real secret env var name."""
    try:
        if ENV_DUMP_RE.search(command):
            return True
        return any(name in command for name in sensitive_env_names())
    except Exception:
        return True  # fail safe


def sanitized_env() -> dict:
    """A copy of the environment with secret-named variables removed, for a subprocess to inherit."""
    return {k: v for k, v in os.environ.items() if not SENSITIVE_ENV_KEY_RE.search(k)}


class EgressBlocked(RuntimeError):
    """The egress sanitizer could not certify a payload, so nothing was sent.

    `redact_secrets` is deliberately best-effort: on the write path, handing back the original
    text is the honest outcome when the scrubber itself breaks. The egress path is the opposite
    trade. A payload that cannot be PROVEN scrubbed does not leave the machine, because the
    failure mode there is a credential in a third party's log rather than a messy memory row."""


def _secret_candidates(extra_secrets: dict[str, str] | None = None) -> dict[str, str]:
    """{value: label} of every secret VALUE to scrub, longest first at the call site.

    Unlike the write-path helper this never swallows: an environment it cannot read is an
    environment it cannot clear, and the caller must treat that as "do not send"."""
    candidates: dict[str, str] = {}
    for name, val in os.environ.items():
        if SENSITIVE_ENV_KEY_RE.search(name) and isinstance(val, str) and len(val) >= _MIN_SECRET_LEN:
            candidates.setdefault(val, name)
    for label, val in (extra_secrets or {}).items():
        if isinstance(val, str) and len(val) >= _MIN_SECRET_LEN:
            candidates.setdefault(val, str(label))
    return candidates


def scrub_strict(text: str, extra_secrets: dict[str, str] | None = None) -> tuple[str, int]:
    """Scrub `text` and report how many replacements were made. Raises on anything it cannot do.

    Same two rules as `redact_secrets` (known secret values, then secret-shaped literals), but
    the return value is a (clean, count) pair and there is no "give up and return the input"
    branch — that branch is exactly what an egress path must not have."""
    if not isinstance(text, str):
        raise TypeError(f"expected str, got {type(text).__name__}")
    if not text:
        return text, 0
    out, count = text, 0
    for value in sorted(_secret_candidates(extra_secrets), key=len, reverse=True)[:_MAX_SECRETS]:
        hits = out.count(value)
        if hits:
            out = out.replace(value, REDACTED)
            count += hits
    out, hits = SECRET_VALUE_RE.subn(REDACTED, out)
    count += hits
    out, hits = SECRET_ASSIGNMENT_RE.subn(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}{m.group(3)}", out)
    return out, count + hits


def egress_sanitize(messages, *, extra_secrets: dict[str, str] | None = None,
                    ) -> tuple[list[dict], int]:
    """The single sanitizer every outbound model prompt passes through. Fail-closed.

    Returns `(clean_messages, redaction_count)`. Raises `EgressBlocked` — never returns the
    original payload — when the input has a shape this function cannot fully inspect, or when
    the scrubber itself fails. Callers must let that exception stop the call: a prompt that was
    not certified must not reach the provider.

    Handles the two content shapes the OpenAI-compatible API accepts (a plain string, and the
    multimodal list of parts). An unrecognised shape is a refusal, not a pass-through, because
    the one thing this function may never do is forward something it did not read.

    Idempotent: running it over already-sanitized messages is a no-op that reports 0, which is
    what lets a second pass sit immediately in front of the HTTP call as a backstop."""
    try:
        if isinstance(messages, (str, bytes, dict)) or not isinstance(messages, (list, tuple)):
            raise TypeError(f"messages must be a list, got {type(messages).__name__}")
        clean: list[dict] = []
        total = 0
        for i, message in enumerate(messages):
            if not isinstance(message, dict):
                raise TypeError(f"message {i} is {type(message).__name__}, not a mapping")
            out = dict(message)
            if "content" in out:
                content = out["content"]
                if isinstance(content, str):
                    scrubbed, hits = scrub_strict(content, extra_secrets)
                    out["content"] = scrubbed
                    total += hits
                elif content is None:
                    pass
                elif isinstance(content, (list, tuple)):
                    parts = []
                    for part in content:
                        if not isinstance(part, dict):
                            raise TypeError(f"message {i} has a content part that is "
                                            f"{type(part).__name__}, not a mapping")
                        piece = dict(part)
                        if isinstance(piece.get("text"), str):
                            scrubbed, hits = scrub_strict(piece["text"], extra_secrets)
                            piece["text"] = scrubbed
                            total += hits
                        elif "text" in piece:
                            raise TypeError(f"message {i} has a non-string text part")
                        parts.append(piece)
                    out["content"] = parts
                else:
                    raise TypeError(f"message {i} content is {type(content).__name__}")
            clean.append(out)
        return clean, total
    except EgressBlocked:
        raise
    except Exception as exc:
        raise EgressBlocked(
            f"egress sanitizer could not certify the outbound payload "
            f"({type(exc).__name__}: {exc}); nothing was sent") from exc


def redact_secrets(text: str, extra_secrets: dict[str, str] | None = None) -> str:
    """Replace secret VALUES in `text` with a marker so they never reach a model or the memory store.

    Covers: literal values of secret-named environment variables, any `extra_secrets`
    ({label: value} — e.g. broker-injected credentials that are not in os.environ), and
    secret-shaped content (private-key headers, cloud key prefixes, JWTs). Longest value
    first, so a secret that contains a shorter one is not left partially disclosed.

    Total by design: this is a best-effort scrubber on the write path, and returning the
    original text is the honest outcome when there is nothing to match. Callers that need a
    hard guarantee must not rely on this alone (see `policy.is_sensitive`, which gates the
    action itself rather than trying to sanitize its payload)."""
    if not text:
        return text
    try:
        candidates: dict[str, str] = {}
        for name in sensitive_env_names():
            val = os.environ.get(name, "")
            if isinstance(val, str) and len(val) >= _MIN_SECRET_LEN:
                candidates.setdefault(val, name)
        for label, val in (extra_secrets or {}).items():
            if isinstance(val, str) and len(val) >= _MIN_SECRET_LEN:
                candidates.setdefault(val, str(label))
        out = text
        for value in sorted(candidates, key=len, reverse=True)[:_MAX_SECRETS]:
            out = out.replace(value, REDACTED)
        return SECRET_VALUE_RE.sub(REDACTED, out)
    except Exception:
        return text
