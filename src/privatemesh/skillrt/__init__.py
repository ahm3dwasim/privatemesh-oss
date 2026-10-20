"""skillrt — the runtime that makes a GENERATED skill safe to run.

A generated skill is untrusted Python. It is allowed to COMPUTE: a pure function over structured
inputs, e.g. rank meeting slots against venue evidence and a busy/free boundary. It is not allowed
a network, a filesystem outside a scratch directory, credentials, or a subprocess, and its only
outputs are a JSON result and stdout. Every external effect is a PROPOSAL returned to the trusted
broker, never performed by the skill.

The four pieces, in the order a skill passes through them:

  manifest.SkillManifest   what the skill declares, and the hash of (code, manifest, tests) that is
                           its identity — a new identity is a new approval
  runner.Runner            how it is executed: local isolation, or a Token Factory Sandbox
  harness.run_harness      whether it is any good: its own tests plus a held-out set it never saw
  proposals.submit         what it may ask for: effects inside its manifest, decided by the broker

The honest claim is the one in the shootout: *within this isolated runtime, external effects must
pass the broker, which rejects effects outside the approved scope.* Not "cannot exceed its
manifest, provably" — `runner` says exactly what the local guard does and does not cover.
"""
from __future__ import annotations

from .harness import (CaseResult, HarnessReport, HeldOutTestsInsideSkill, run_harness,
                      source_of)
from .manifest import KNOWN_EFFECTS, ManifestError, SkillManifest
from .proposals import Proposal, admit, submit
from .runner import (
    ERROR,
    OK,
    TIMEOUT,
    VIOLATION,
    ContreeClient,
    ContreeRunner,
    Limits,
    LocalIsolatedRunner,
    Runner,
    RunResult,
    RunSpec,
)
from .violations import KINDS, SkillViolation, Violation

__all__ = [
    "CaseResult", "ContreeClient", "ContreeRunner", "ERROR", "HarnessReport",
    "HeldOutTestsInsideSkill", "KINDS", "KNOWN_EFFECTS", "Limits", "LocalIsolatedRunner",
    "ManifestError", "OK", "Proposal", "RunResult", "RunSpec", "Runner", "SkillManifest",
    "SkillViolation", "TIMEOUT", "VIOLATION", "Violation", "admit", "run_harness", "source_of",
    "submit",
]
