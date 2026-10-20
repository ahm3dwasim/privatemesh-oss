"""The named violations a generated skill can commit.

A guard that says "blocked" is not evidence. The escape tests assert on the *kind*, so a change
that stops blocking sockets fails a specific named test rather than a generic one — and the report
the owner reads can say "tried to open a network connection" instead of "failed".

The child process re-declares this vocabulary in `_bootstrap.py`, which cannot import this module
(`-I -S` strips site-packages and the enforcement code deliberately does not ship with the skill).
`tests/test_skillrt_runner.py::test_the_child_and_parent_agree_on_the_violation_vocabulary` keeps
the two honest.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Every way a skill can be stopped. Kept small and specific on purpose.
KINDS = (
    "network",          # sockets, DNS, urllib, http
    "subprocess",       # subprocess.Popen, os.system
    "process_control",  # fork / exec / spawn
    "filesystem",       # open() outside the scratch dir, rename, mkdir, listdir
    "import",           # ctypes, socket, ssl, urllib, http, requests, httpx
    "environment",      # reading os.environ for keys
    "memory",           # reading a preloaded module's globals for secrets (see runner docs)
    "resources",        # CPU / memory / wall-clock caps
    "proposal",         # proposing an effect outside the manifest, or an unusable proposal
    "manifest",         # inputs or a result that do not fit the declared schema
)


@dataclass(frozen=True)
class Violation:
    """One refused action, named. `event` is the CPython audit event that caught it, when there
    was one — it is what makes the block auditable rather than merely asserted."""

    kind: str
    detail: str
    event: str = ""

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}" + (f" [{self.event}]" if self.event else "")


class SkillViolation(RuntimeError):
    """Raised inside the isolated child. Carries the structured violation for the report."""

    def __init__(self, kind: str, detail: str, *, event: str = "") -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown violation kind {kind!r}")
        self.violation = Violation(kind, detail, event)
        super().__init__(str(self.violation))
