# Ported from the author's prior private assistant (Jarvis); adapted for PrivateMesh.
"""The policy engine — this repo's authority layer, and it is deterministic code, never a model.

Ported from the prior assistant's `kernel/policy.py` (the decision table and `PolicyResult`),
the owner-ceiling core of its `kernel/autonomy.py`, and the `Tier` / `classify_tool` /
`is_sensitive` classifiers from its constitution module. Everything agent-SDK specific
in that module — the PreToolUse hook, `HookMatcher`, `build_permission_hooks` — is NOT ported:
PrivateMesh has no such SDK, and its enforcement point is the broker.

The classifier half says what an action IS (READ / DRAFT / ACT, and whether it is sensitive).
`evaluate` turns that plus the owner's autonomy policy into a decision:

  BLOCKED risk                        -> DENY
  sensitive OR risk >= HIGH           -> NEEDS_APPROVAL   (DENY when no human is present)
  READ / DRAFT tier                   -> ALLOW
  ACT tier (routine)                  -> ALLOW iff the owner opted in, else NEEDS_APPROVAL

Two invariants the autonomy policy can never weaken, and which come first in `evaluate`:
a BLOCKED tool is always denied, and a SENSITIVE action always needs the owner. Overrides
may only tighten, or promote a non-sensitive action.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .redact import SECRET_PATH_RE, command_touches_secret, path_is_sensitive
from .skills import Risk, Tool


class Tier(str, Enum):
    READ = "read"
    DRAFT = "draft"
    ACT = "act"


# Shell/exec tools are the ones an unattended server may never run at all (belt-and-suspenders
# behind the "no human present -> DENY" rule).
ACT_TOOL_NAMES = ("Bash", "KillShell", "KillBash")
# "Draft" is PrivateMesh's own gate for skills that produce something locally without delivering
# it (email_draft) — the same tier as a file Write, for the same reason.
_DRAFT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Draft"}
_ACT_PATTERN = re.compile(
    r"(send|email|mail_send|delete|remove|trash|publish|post|merge|push|deploy|"
    r"transfer|pay|purchase|checkout|charge|revoke|grant)",
    re.IGNORECASE,
)

_SHELL_TOOLS = {"Bash", "KillShell", "KillBash"}

# Touching secrets/credentials/keys — reading OR writing OR referencing in a command.
_SECRET_RE = re.compile(
    r"(\.env\b|\.pem\b|\.key\b|id_rsa|id_ed25519|\.ssh/|authorized_keys|\.aws\b|\.netrc|"
    r"\.credentials|/etc/shadow|credential|secret|password|passwd|\bapi[_-]?key\b|\btoken\b|"
    r"[a-z0-9]+_(?:api_?key|token|secret|password|credential|key)\b|"
    r"keychain|private[_-]?key)",
    re.IGNORECASE,
)

# Shell commands that are destructive, escalate privilege, reach the network, install code,
# change system/security config, or hide intent behind an inline interpreter.
_SENSITIVE_BASH_RE = re.compile(
    r"(\bsudo\b|\bsu\b|\bdoas\b|\bpkexec\b|"
    r"\brm\s+-[a-z]*[rf]|\brmdir\b|\bdd\b|\bmkfs|\bshred\b|\btruncate\b|>\s*/dev/|:\(\)\s*\{|"
    r"\bdropdb\b|drop\s+(table|database)|"
    r"\bcurl\b|\bwget\b|\bnc\b|\bncat\b|\bscp\b|\bssh\b|\bsftp\b|\bftp\b|\btelnet\b|"
    r"\brsync\b[^|]*:|\|\s*(bash|sh)\b|base64\s+-d|"
    r"\bufw\b|\biptables\b|\bnft\b|\bsystemctl\b|\bservice\s|\buseradd\b|\buserdel\b|"
    r"\busermod\b|\bchpasswd\b|visudo|/etc/|\bcrontab\b|\bmount\b|setenforce|ssh-keygen|"
    r"\bchmod\b|\bchown\b|"
    r"\bapt(-get)?\s+(install|remove|purge)|\bpip3?\s+install|\bnpm\s+(install|i|ci)\b|"
    r"\byarn\s+add|\bbrew\s+install|\bgem\s+install|\bgo\s+install|\bcargo\s+install|"
    r"python[0-9.]*\s+-c\b|perl\s+-e\b|ruby\s+-e\b|node\s+-e\b|\beval\b|(bash|sh)\s+-c\b|"
    r"\benv\b|\bprintenv\b|export\s+-p|declare\s+-x|/proc/[^\s]*/environ|"
    r"git\s+push|--force\b|git\s+reset\s+--hard|git\s+clean\s+-[a-z]*f)",
    re.IGNORECASE,
)

# Writing to security-critical paths is always sensitive.
_SECURITY_PATH_RE = re.compile(
    r"(privatemesh/(policy|broker|approval|audit|skills|agent|api|memory)\.py|"
    r"/etc/|sudoers|authorized_keys|\.service\b|crontab|systemd)",
    re.IGNORECASE,
)

# Gate names whose target is a path the caller wants to write/modify.
_WRITE_GATES = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Keys whose value is the tool's primary target, for the extra sensitivity checks below.
_TARGET_KEYS = ("path", "file_path", "command", "url")

# The autonomy file IS the permission ceiling. No tool may write it — the owner edits it by hand —
# so a single misread approval can never let the model rewrite its own limits.
CEILING_FILENAME = "autonomy_policy.json"


def classify_tool(tool_name: str) -> Tier:
    """READ / DRAFT / ACT from the tool's gate name. Pure; never raises."""
    base = (tool_name or "").split("__")[-1]  # strip mcp-style server prefixes
    if base in ACT_TOOL_NAMES or _ACT_PATTERN.search(tool_name or ""):
        return Tier.ACT
    if base in _DRAFT_TOOLS:
        return Tier.DRAFT
    return Tier.READ


def _target(tool_name: str, tool_input: dict) -> str:
    for key in ("command", "file_path", "path", "pattern", "url"):
        val = (tool_input or {}).get(key)
        if isinstance(val, str):
            return val
    return " ".join(str(v) for v in (tool_input or {}).values())


def _path_target(tool_input: dict) -> str:
    """The target, but ONLY from a path/URL-shaped key — never from a prose field.

    A READ or DRAFT whose payload is free text (an email body, a note) must not be judged by the
    words inside it: a draft about "resetting your password" is not a credential action, and
    refusing it would be friction with no security gain. Paths and URLs are where a secret
    actually lives, so those are what these tiers look at."""
    for key in ("path", "file_path", "url", "pattern"):
        val = (tool_input or {}).get(key)
        if isinstance(val, str):
            return val
    return ""


def is_sensitive(tool_name: str, tool_input: dict) -> bool:
    """True when an action must be confirmed by a human even if routine ACTs are auto-approved.

    Sensitive = any external non-shell effect (send/publish/delete); a shell command that is
    destructive, escalates privilege, reaches the network, installs code, or touches secrets; or a
    WRITE to a security-critical or secret path. Plain READS are never sensitive — reading stays on
    the machine, and any exfiltration is caught at the ACT layer, so approving every read is pure
    friction with no security gain."""
    base = (tool_name or "").split("__")[-1]
    tier = classify_tool(tool_name)
    target = _target(tool_name, tool_input)

    if tier is Tier.ACT and base not in _SHELL_TOOLS:
        return True
    if base in _SHELL_TOOLS:
        return bool(_SENSITIVE_BASH_RE.search(target) or _SECRET_RE.search(target))
    path_target = _path_target(tool_input)
    if tier is Tier.DRAFT and path_target:
        return bool(_SECURITY_PATH_RE.search(path_target) or _SECRET_RE.search(path_target))
    if tier is Tier.READ and path_target and _SECRET_RE.search(path_target):
        return True
    return False


class Decision(str, Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: str
    risk: Risk
    tier: Tier
    sensitive: bool

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


_MEDIUM_ACTIONS = {"auto", "approve"}
_OVERRIDE_ACTIONS = {"auto", "approve", "deny"}


@dataclass(frozen=True)
class AutonomyPolicy:
    """The owner's editable ceiling. Loading is total: a missing or malformed file, or an invalid
    value, falls back to the safe default (ask). Nothing here can loosen a hard rule — see the
    two invariants that run first in `evaluate`."""

    medium_action: str = "approve"
    tool_overrides: dict = field(default_factory=dict)

    @property
    def medium_auto(self) -> bool:
        return self.medium_action == "auto"

    def override_for(self, tool_name: str) -> str | None:
        val = self.tool_overrides.get(tool_name)
        return val if val in _OVERRIDE_ACTIONS else None


def load_autonomy_policy(path) -> AutonomyPolicy:
    """Read the owner's policy JSON, or safe defaults. Never raises."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return AutonomyPolicy()
    if not isinstance(raw, dict):
        return AutonomyPolicy()
    medium = raw.get("medium_action")
    medium = medium if isinstance(medium, str) and medium in _MEDIUM_ACTIONS else "approve"
    overrides_raw = raw.get("tool_overrides")
    overrides_raw = overrides_raw if isinstance(overrides_raw, dict) else {}
    overrides = {str(k): v for k, v in overrides_raw.items()
                 if isinstance(v, str) and v in _OVERRIDE_ACTIONS}
    return AutonomyPolicy(medium_action=medium, tool_overrides=overrides)


def _arg_target(arguments: dict) -> str:
    for key in _TARGET_KEYS:
        val = (arguments or {}).get(key)
        if isinstance(val, str):
            return val
    return " ".join(str(v) for v in (arguments or {}).values() if isinstance(v, str))


def evaluate(tool: Tool, arguments: dict, *, interactive: bool,
             autonomy: AutonomyPolicy | None = None) -> PolicyResult:
    """Decide whether `tool(arguments)` may run. Pure function; never raises.

    `interactive=False` means no human can be asked right now: a NEEDS_APPROVAL collapses to a
    hard DENY, so an unattended ACT can never silently proceed."""
    if autonomy is None:
        autonomy = AutonomyPolicy()

    tier = classify_tool(tool.gate_name)
    sensitive = is_sensitive(tool.gate_name, arguments)

    # Kernel-level tightening the shared classifier doesn't cover (never loosening):
    #  - reading a secret/credential FILE is not "auto" once an egress tool can carry it off-box;
    #  - writing to a secret/persistence path needs review;
    #  - a shell command that dumps env or names a real secret env var is sensitive.
    target = _arg_target(arguments)
    write_target = _path_target(arguments) or target
    if not sensitive and target:
        if tool.gate_name == "Read" and SECRET_PATH_RE.search(target):
            sensitive = True
        elif tool.gate_name in _WRITE_GATES and write_target and path_is_sensitive(write_target):
            sensitive = True
        elif tool.gate_name == "Bash" and command_touches_secret(target):
            sensitive = True

    def result(decision: Decision, reason: str) -> PolicyResult:
        if decision is Decision.NEEDS_APPROVAL and not interactive:
            return PolicyResult(Decision.DENY,
                                f"{reason} No human is present to confirm (unattended); denied.",
                                tool.risk, tier, sensitive)
        return PolicyResult(decision, reason, tool.risk, tier, sensitive)

    override = autonomy.override_for(tool.name)

    # --- Hard safety first — the autonomy policy can never weaken these. ---
    if tool.risk >= Risk.BLOCKED:
        return result(Decision.DENY, f"'{tool.name}' is BLOCKED by policy and can never run.")
    if tool.gate_name in _WRITE_GATES and CEILING_FILENAME in target.lower():
        return result(Decision.DENY, "The autonomy policy can't be changed by a tool — edit "
                                     f"{CEILING_FILENAME} yourself.")
    if override == "deny":
        return result(Decision.DENY, f"'{tool.name}' is set to 'deny' in your autonomy policy.")
    if sensitive:
        return result(Decision.NEEDS_APPROVAL,
                      f"'{tool.name}' is a sensitive action and needs the owner's approval.")

    # --- Owner overrides for non-sensitive, non-blocked actions. ---
    if override == "approve":
        return result(Decision.NEEDS_APPROVAL, f"'{tool.name}' is set to always ask in your autonomy policy.")
    if override == "auto":
        return result(Decision.ALLOW, f"'{tool.name}' is set to auto-run in your autonomy policy.")

    # --- Standard decision table. ---
    if tool.risk >= Risk.HIGH:
        return result(Decision.NEEDS_APPROVAL,
                      f"'{tool.name}' is {tool.risk.name} risk and needs the owner's approval.")
    if tool.approval_required:
        return result(Decision.NEEDS_APPROVAL,
                      f"'{tool.name}' is marked approval-required and needs the owner's OK.")
    if tier in (Tier.READ, Tier.DRAFT):
        return result(Decision.ALLOW, f"'{tool.name}' is {tier.value}-tier — allowed automatically.")
    # ACT tier, non-sensitive, medium risk (e.g. a routine local write).
    if autonomy.medium_auto:
        return result(Decision.ALLOW, f"'{tool.name}' is routine and auto-run is enabled.")
    return result(Decision.NEEDS_APPROVAL,
                  f"'{tool.name}' is an ACT-tier action and needs the owner's approval.")
