"""LOW 7 — the argument fingerprint is a contract, not a best effort.

`args_fingerprint` used `json.dumps(..., default=str)` with a `repr()` fallback, and the module
called the result an "exact arguments" guarantee. For values outside JSON it was neither exact
nor a guarantee: two different objects whose `repr` agrees — or an integer key that `json` coerces
to a string — produced one authorization identity for two different actions.

The fix is to accept only the JSON value domain, reject everything else, and canonicalise with an
explicit tag per type. `ApprovalError` is imported inside the tests that need it so this module
still collects against pre-fix code, where the raise never happens — that is what makes the red
check an assertion failure rather than an import error.
"""
from __future__ import annotations

import pytest

from privatemesh.approval import ApprovalError, args_fingerprint


def test_a_non_json_value_is_refused_rather_than_stringified():
    """The bypass in one line: an object that renders itself as the approved text."""
    class Sneaky:
        def __repr__(self) -> str:
            return "'the agreed text'"

    with pytest.raises(ApprovalError):
        args_fingerprint("send_message", {"body": Sneaky()})


def test_two_distinct_objects_cannot_share_one_authorization_identity():
    """Both of these repr to the same string. Pre-fix they hashed to the same fingerprint, so
    approving one would have covered the other."""
    class Payload:
        def __repr__(self) -> str:
            return "<payload>"

    class Disguise:
        def __repr__(self) -> str:
            return "<payload>"

    for value in (Payload(), Disguise()):
        with pytest.raises(ApprovalError):
            args_fingerprint("send_message", {"body": value})


def test_an_integer_key_cannot_impersonate_a_string_key():
    """`json.dumps({1: "a"})` and `json.dumps({"1": "a"})` are the same string, and the old
    canonicaliser applied that coercion silently."""
    assert args_fingerprint("s", {"1": "a"})              # the string key is a legal identity
    with pytest.raises(ApprovalError):
        args_fingerprint("s", {1: "a"})                   # the integer key is a different action


def test_non_finite_numbers_are_refused():
    """NaN and the infinities have no exact JSON form, and `json.dumps` writes them as bare
    `NaN`/`Infinity` — tokens no strict parser accepts back."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ApprovalError):
            args_fingerprint("s", {"n": bad})


def test_each_json_type_is_tagged_so_similar_values_do_not_collide():
    values = (1, 1.0, True, "1", [1], {"a": 1})
    fingerprints = {args_fingerprint("s", {"n": v}) for v in values}
    assert len(fingerprints) == len(values)


def test_the_json_domain_itself_is_still_accepted():
    """The rejection must not be so broad that ordinary tool arguments stop working."""
    digest = args_fingerprint("send_message", {
        "to": "alex", "body": "text", "count": 3, "urgent": False, "note": None,
        "tags": ["a", "b"], "meta": {"nested": {"deep": [1, 2.5, True, None]}}})
    assert isinstance(digest, str) and len(digest) == 64


def test_a_tuple_is_canonicalised_as_the_array_it_serialises_to():
    assert args_fingerprint("s", {"xs": (1, 2)}) == args_fingerprint("s", {"xs": [1, 2]})


def test_the_stored_request_does_not_alias_the_callers_arguments(approval_store):
    """The copy at request time was shallow, so a nested object stayed shared: everything the
    owner approved could be rewritten afterwards by whoever held the original."""
    args = {"to": "alex", "nested": {"body": "the agreed text"}, "tags": ["a"]}
    req = approval_store.request("t", "send_message", args)

    args["nested"]["body"] = "MUTATED AFTER THE OWNER APPROVED IT"
    args["to"] = "someone-else"
    args["tags"].append("c")

    assert req.arguments == {"to": "alex", "nested": {"body": "the agreed text"}, "tags": ["a"]}


def test_a_request_with_an_unrepresentable_argument_is_refused(approval_store):
    with pytest.raises(ApprovalError):
        approval_store.request("t", "send_message", {"body": object()})


def test_deeply_nested_arguments_are_bounded(approval_store):
    """A hostile payload must not turn validation into a stack overflow."""
    deep: dict = {"x": 1}
    for _ in range(200):
        deep = {"n": deep}
    with pytest.raises(ApprovalError, match="nested too deeply"):
        approval_store.request("t", "send_message", deep)
