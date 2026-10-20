"""The skill manifest — what a generated skill declares about itself, and the hash that is its
identity.

Two claims live here, and both exist to make a *reusable* capability safe rather than a one-off
prompt:

**Identity is the hash of (code, manifest, tests).** Not the name, not the version string — the
bytes. A generated skill is approved once and then re-run in a later session on changed inputs;
the thing that was approved has to be the thing that runs. Change one line of the skill, one line
of its tests, or one field of this manifest and the identity changes, the old approval no longer
binds, and the owner is asked again. `SkillManifest.identity()` is what the approval is keyed on.

**A manifest is a budget, not documentation.** `reads` and `proposes` are the two directions in
which a skill could reach past what the owner agreed to: reading an input it never declared, or
proposing an effect it was never granted. Both are enforced by the runtime — `validate_inputs`
before the call, `proposals.admit` after it — never by trusting the skill to behave.

Deliberately NOT here: any notion of the skill being *safe*. A manifest is a claim by the
generator. The runtime, the harness and the broker are what make the claim true or visible.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field

#: The effect kinds a skill may propose. These are the product's vocabulary (see docs/API_V2.md),
#: not the broker's skill names — `proposals.py` owns the mapping between the two, so a skill
#: proposes "email_reply" and never names a broker skill.
KNOWN_EFFECTS = frozenset({"calendar_update", "email_reply"})

#: JSON-Schema types the runtime checks without pulling in a schema library.
_JSON_TYPES = {
    "string": str, "number": (int, float), "integer": int, "boolean": bool,
    "array": list, "object": dict,
}


class ManifestError(ValueError):
    """The manifest itself is malformed, or an input/result does not fit it."""


def canonical(obj) -> str:
    """One byte string per value. The identity hash depends on this being stable across runs and
    machines, so keys are sorted and separators are fixed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _check_schema(schema: dict, what: str) -> None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ManifestError(f"{what} must be a JSON Schema object, got {schema!r}")
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        raise ManifestError(f"{what} must declare at least one property")
    declared = set(props)
    for req in schema.get("required") or ():
        if req not in declared:
            raise ManifestError(f"{what}: required field {req!r} is not in properties")


def _check_value(value, spec: dict, where: str) -> None:
    """A deliberately small type check. It is not a JSON-Schema implementation and does not try to
    be: it catches the generator mistake that matters (a field of the wrong shape crossing the
    boundary), and anything subtler is the held-out tests' job."""
    want = spec.get("type")
    if want is None:
        return
    if want == "integer" and isinstance(value, bool):
        raise ManifestError(f"{where}: expected integer, got boolean")
    expected = _JSON_TYPES.get(want)
    if expected is None:
        return
    if not isinstance(value, expected):
        raise ManifestError(f"{where}: expected {want}, got {type(value).__name__}")
    if want == "array" and "items" in spec:
        for i, item in enumerate(value):
            _check_value(item, spec["items"], f"{where}[{i}]")


@dataclass(frozen=True)
class SkillManifest:
    """One learned capability, declared.

    `reads` is the only part of the outside world the skill is handed, and it is checked in both
    directions at the call: an input the skill did not declare is refused, so a caller cannot widen
    a skill's view by passing extra keys, and the skill cannot ask for more at runtime.
    """

    name: str
    version: str
    purpose: str
    inputs: dict = field(default_factory=dict)     # JSON Schema for the one argument object
    outputs: dict = field(default_factory=dict)    # JSON Schema for the returned object
    reads: tuple[str, ...] = ()                    # declared input field names, only
    proposes: tuple[str, ...] = ()                 # effect kinds it may propose
    expires: str = ""                              # ISO-8601 UTC; "" = never
    module: str = ""                               # defaults to `name`
    entry: str = "run"                             # the function the runner calls
    #: The owner's own sentence, for a skill they TAUGHT (Teach Mode); "" for a skill the build
    #: knew how to ask for.
    #:
    #: It lives on the manifest rather than beside it because `as_dict` feeds the identity hash: the
    #: owner approves a rule and the code that implements it, and a rule that could be swapped under
    #: an unchanged hash would be an approval binding to something other than what was read. It is
    #: also the reason a re-taught rule with identical bytes is still a distinct artifact from a
    #: different rule with identical bytes.
    rule: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            raise ManifestError(f"skill name {self.name!r} must be a Python identifier")
        if not self.version:
            raise ManifestError("a manifest needs a version")
        _check_schema(self.inputs, "inputs")
        _check_schema(self.outputs, "outputs")
        declared = set(self.inputs["properties"])
        undeclared = [f for f in self.reads if f not in declared]
        if undeclared:
            raise ManifestError(f"reads declares fields not in inputs: {undeclared}")
        if set(self.reads) != declared:
            raise ManifestError(
                f"reads must name every input field and nothing else; "
                f"inputs={sorted(declared)} reads={sorted(self.reads)}")
        unknown = [e for e in self.proposes if e not in KNOWN_EFFECTS]
        if unknown:
            raise ManifestError(f"unknown effect kind(s) {unknown}; known: {sorted(KNOWN_EFFECTS)}")
        if self.expires:
            try:
                _dt.datetime.fromisoformat(self.expires.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ManifestError(f"expires must be ISO-8601: {exc}") from exc

    # ---- identity ------------------------------------------------------------------------
    @property
    def module_name(self) -> str:
        return self.module or self.name

    def identity(self, code: str, tests: str = "", *, held_out: str = "") -> str:
        """The hash an approval binds to: the skill's code, this manifest, and its tests.

        `held_out` is included when the runtime can see it, so an approval cannot survive a change
        to the tests the coder was graded against either."""
        digest = hashlib.sha256()
        digest.update(canonical({"code": code, "manifest": self.as_dict(), "tests": tests,
                                 "held_out": held_out}).encode("utf-8"))
        return "sha256:" + digest.hexdigest()

    def as_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "purpose": self.purpose,
                "inputs": self.inputs, "outputs": self.outputs, "reads": list(self.reads),
                "proposes": list(self.proposes), "expires": self.expires,
                "module": self.module_name, "entry": self.entry, "rule": self.rule}

    def to_json(self) -> str:
        return canonical(self.as_dict())

    # ---- the two directions out of the skill ---------------------------------------------
    def expired(self, now: _dt.datetime | None = None) -> bool:
        if not self.expires:
            return False
        now = now or _dt.datetime.now(_dt.timezone.utc)
        when = _dt.datetime.fromisoformat(self.expires.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        return now >= when

    def validate_inputs(self, provided: dict) -> dict:
        """Refuse anything the skill did not declare. Returns the accepted inputs.

        The narrow direction is the point: `reads` is what the owner consented to hand over, so a
        caller passing a field the skill never declared is refused rather than silently delivered.
        """
        provided = dict(provided or {})
        extra = sorted(set(provided) - set(self.reads))
        if extra:
            raise ManifestError(f"inputs {extra} are not in the manifest's declared reads "
                                f"{sorted(self.reads)}")
        missing = [f for f in (self.inputs.get("required") or ()) if provided.get(f) in (None, "")]
        if missing:
            raise ManifestError(f"missing required input(s): {', '.join(missing)}")
        for key, value in provided.items():
            _check_value(value, self.inputs["properties"][key], f"input {key!r}")
        return provided

    def validate_result(self, result) -> dict:
        """Refuse a result the manifest never promised. A skill that returns a surprise field is
        not a skill whose output the broker should be reading."""
        if not isinstance(result, dict):
            raise ManifestError(f"a skill must return an object, got {type(result).__name__}")
        extra = sorted(set(result) - set(self.outputs["properties"]))
        if extra:
            raise ManifestError(f"result field(s) {extra} are not in the declared outputs "
                                f"{sorted(self.outputs['properties'])}")
        missing = [f for f in (self.outputs.get("required") or ()) if f not in result]
        if missing:
            raise ManifestError(f"result is missing declared output(s): {', '.join(missing)}")
        for key, value in result.items():
            _check_value(value, self.outputs["properties"][key], f"output {key!r}")
        return result
