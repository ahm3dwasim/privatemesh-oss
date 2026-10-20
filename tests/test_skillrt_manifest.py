"""The manifest is a budget, not documentation: identity, and the two directions out of a skill.

The identity tests are the ones that carry the product claim. "Teach it once, keep control" only
works if the thing approved in one session is provably the thing that runs in the next — so any
change to the code, the manifest or the tests has to produce a different identity, without anyone
remembering to bump a version string.
"""
from __future__ import annotations

import datetime as dt

import pytest

from privatemesh.skillrt.manifest import KNOWN_EFFECTS, ManifestError, SkillManifest

SLOT_INPUTS = {
    "type": "object",
    "properties": {
        "slots": {"type": "array", "items": {"type": "string"}},
        "busy": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["slots"],
}
SLOT_OUTPUTS = {
    "type": "object",
    "properties": {"ranked": {"type": "array", "items": {"type": "string"}},
                   "reason": {"type": "string"}},
    "required": ["ranked"],
}


def manifest(**kw) -> SkillManifest:
    base = dict(name="rank_slots", version="1.0.0", purpose="Rank meeting slots around a boundary",
                inputs=SLOT_INPUTS, outputs=SLOT_OUTPUTS, reads=("slots", "busy"), proposes=())
    base.update(kw)
    return SkillManifest(**base)


# ---------------------------------------------------------------- identity ---------------------
def test_identity_changes_when_the_code_changes():
    m = manifest()
    assert m.identity("def run(i): return {}", "t") != m.identity("def run(i): return {1: 2}", "t")


def test_identity_changes_when_the_manifest_changes():
    assert manifest().identity("code", "t") != manifest(purpose="something else").identity("code", "t")


def test_identity_changes_when_the_tests_change():
    """A skill whose tests were relaxed is a different skill, even though its code is identical."""
    m = manifest()
    assert m.identity("code", "def test_a(): pass") != m.identity("code", "def test_a(): assert 0")


def test_identity_changes_when_the_held_out_tests_change():
    """The held-out set is part of the identity too. Otherwise an approval could outlive a change
    to the very tests that justified it."""
    m = manifest()
    assert m.identity("c", "t", held_out="a") != m.identity("c", "t", held_out="b")


def test_identity_is_stable_across_runs_and_dict_ordering():
    """Two dicts with the same content in a different insertion order are the same manifest, and an
    identity that depended on insertion order would invalidate approvals at random."""
    a = manifest(inputs={"type": "object", "properties": {"slots": {"type": "array"},
                                                          "busy": {"type": "array"}},
                         "required": ["slots"]})
    b = manifest(inputs={"required": ["slots"],
                         "properties": {"busy": {"type": "array"}, "slots": {"type": "array"}},
                         "type": "object"})
    assert a.identity("code", "tests") == b.identity("code", "tests")


# ---------------------------------------------------------------- well-formedness ---------------
def test_reads_must_match_the_declared_inputs_exactly():
    """`reads` is what the owner consented to hand over. A manifest that declares an input the
    skill does not read — or reads a field it did not declare — is rejected at construction."""
    with pytest.raises(ManifestError, match="reads must name every input field"):
        manifest(reads=("slots",))
    with pytest.raises(ManifestError, match="reads declares fields not in inputs"):
        manifest(reads=("slots", "busy", "secret_calendar"))


def test_an_unknown_effect_kind_is_refused():
    with pytest.raises(ManifestError, match="unknown effect kind"):
        manifest(proposes=("wire_transfer",))
    assert manifest(proposes=("email_reply",)).proposes == ("email_reply",)
    assert "email_reply" in KNOWN_EFFECTS


def test_a_name_that_is_not_an_identifier_is_refused():
    """The name becomes the module name and the sys.path entry, so it has to be importable."""
    with pytest.raises(ManifestError, match="Python identifier"):
        manifest(name="../../etc/passwd")


def test_expiry_is_optional_and_parsed():
    assert manifest().expired() is False
    assert manifest(expires="2020-01-01T00:00:00Z").expired() is True
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).isoformat()
    assert manifest(expires=later).expired() is False
    with pytest.raises(ManifestError, match="ISO-8601"):
        manifest(expires="next tuesday")


# ---------------------------------------------------------------- the two directions ------------
def test_inputs_outside_the_declared_reads_are_refused():
    """The narrow direction: a caller cannot widen a skill's view by passing extra keys."""
    m = manifest()
    assert m.validate_inputs({"slots": ["15:00"], "busy": []}) == {"slots": ["15:00"], "busy": []}
    with pytest.raises(ManifestError, match="not in the manifest's declared reads"):
        m.validate_inputs({"slots": ["15:00"], "protected_commitment": {"title": "therapy"}})


def test_missing_required_inputs_are_refused():
    with pytest.raises(ManifestError, match="missing required input"):
        manifest().validate_inputs({})


def test_input_types_are_checked():
    with pytest.raises(ManifestError, match="expected array, got str"):
        manifest().validate_inputs({"slots": "15:00"})


def test_a_result_with_an_undeclared_field_is_refused():
    """A skill that returns a surprise field is not a skill whose output the broker should read."""
    m = manifest()
    assert m.validate_result({"ranked": ["15:00"], "reason": "fits"})["ranked"] == ["15:00"]
    with pytest.raises(ManifestError, match="not in the declared outputs"):
        m.validate_result({"ranked": [], "exfiltrated": "the private reason"})


def test_a_result_missing_a_declared_output_is_refused():
    with pytest.raises(ManifestError, match="missing declared output"):
        manifest().validate_result({"reason": "fits"})


def test_to_json_is_canonical():
    m = manifest()
    assert m.to_json() == m.to_json()
    assert '"name":"rank_slots"' in m.to_json()
