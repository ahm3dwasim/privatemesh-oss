"""The skill library — identity discipline, in one file.

An approval binds to a hash. Everything here is a way that promise could quietly stop being true:
a row edited in the database, a second artifact under the old identity, two live versions of one
name, a revoked skill that still runs. Each test names the one that it pins.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

from privatemesh.library import ACTIVE, REVOKED, LibraryError, SkillLibrary
from privatemesh.skillrt import SkillManifest

CODE = "def run(inputs):\n    return {'ok': True}\n"
OTHER = "def run(inputs):\n    return {'ok': False}\n"
TESTS = "import thing\n\n\ndef test_it():\n    assert thing.run({})['ok'] is True\n"
HELD_OUT = "import thing\n\n\ndef test_held():\n    assert thing.run({})['ok'] is True\n"


def manifest(version: str = "1", **kw) -> SkillManifest:
    kw.setdefault("name", "thing")
    return SkillManifest(version=version, purpose="a thing",
                         inputs={"type": "object", "properties": {"a": {"type": "integer"}}},
                         outputs={"type": "object", "properties": {"ok": {"type": "boolean"}}},
                         reads=("a",), **kw)


def at(seconds: int):
    base = _dt.datetime(2026, 9, 18, 12, 0, tzinfo=_dt.timezone.utc)
    return lambda: base + _dt.timedelta(seconds=seconds)


def installed(library: SkillLibrary, **kw) -> dict:
    return library.install(manifest=kw.pop("manifest", manifest()), code=kw.pop("code", CODE),
                           tests=kw.pop("tests", TESTS), held_out=kw.pop("held_out", HELD_OUT),
                           purpose_plain=kw.pop("purpose_plain", "does a thing"))


# ---------------------------------------------------------------- writing ---------------------
def test_install_recomputes_the_identity_rather_than_trusting_the_caller():
    """The whole safety story in one line: the hash the owner approved is derived from the bytes
    about to be stored, not accepted from whoever is storing them."""
    library = SkillLibrary()
    row = installed(library)
    assert row["identity"] == manifest().identity(CODE, TESTS, held_out=HELD_OUT)
    assert row["identity"].startswith("sha256:")
    assert library.get(row["identity"])["code"] == CODE


def test_one_byte_of_code_is_a_new_identity_and_therefore_a_new_approval():
    """The claim the whole product rests on, at the smallest possible change: an approval is of
    BYTES. Change a constant, a comment's content, one character of the code, and the hash the
    owner agreed to no longer describes anything that exists — so nothing they approved runs."""
    library = SkillLibrary()
    first = installed(library)
    second = installed(library, manifest=manifest("2"), code=CODE.replace("True", "True "),
                       tests=TESTS, held_out=HELD_OUT)
    assert second["identity"] != first["identity"]
    assert library.active("thing")["identity"] == second["identity"]
    assert library.get(first["identity"])["state"] == REVOKED


def test_installing_a_new_version_revokes_the_one_it_replaces():
    """One name, one live version. A stale approval must never quietly start binding to new code."""
    library = SkillLibrary()
    first = installed(library, manifest=manifest("1"))
    second = installed(library, manifest=manifest("2"), code=OTHER)
    assert library.active("thing")["identity"] == second["identity"]
    assert library.get(first["identity"])["state"] == REVOKED
    assert library.get(first["identity"]) is not None, "the withdrawn approval stays readable"


def test_install_is_idempotent_and_does_not_revoke_itself():
    """Approving the same bytes twice is the same skill, not a new version."""
    library = SkillLibrary()
    first = installed(library)
    again = installed(library)
    assert again["identity"] == first["identity"]
    assert library.active("thing")["state"] == ACTIVE


# ---------------------------------------------------------------- reading ----------------------
def test_load_refuses_a_row_whose_bytes_no_longer_hash_to_its_identity(tmp_path):
    """The failure this pins is not hypothetical for a file-backed library: a row edited by hand,
    by a migration, or by something that reached the database. Recomputed on the way OUT, so what
    runs is what was approved."""
    library = SkillLibrary()
    row = installed(library)
    library._conn.execute("UPDATE skills SET code=? WHERE identity=?", (OTHER, row["identity"]))
    with pytest.raises(LibraryError, match="does not hash to its stored identity"):
        library.load("thing")


def test_load_refuses_a_revoked_skill():
    library = SkillLibrary()
    installed(library)
    assert library.revoke("thing") is True
    with pytest.raises(LibraryError, match="revoked"):
        library.load("thing")


def test_a_revoked_skill_is_still_summarised_with_its_state():
    """The owner's library is a record. Hiding a revoked entry would hide the approval that was
    withdrawn, which is the one row they most need to see."""
    library = SkillLibrary()
    installed(library)
    library.revoke("thing")
    rows = library.summaries()
    assert [r["state"] for r in rows] == [REVOKED]
    assert rows[0]["name"] == "thing"


def test_serves_is_false_until_something_is_installed_and_false_again_after_revocation():
    library = SkillLibrary()
    assert library.serves("thing") is False
    installed(library)
    assert library.serves("thing") is True
    library.revoke("thing")
    assert library.serves("thing") is False


def test_record_use_counts_only_the_live_version():
    library = SkillLibrary(now=at(0))
    first = installed(library)
    library.record_use("thing")
    assert library.active("thing")["uses"] == 1
    assert library.active("thing")["last_used_at"].startswith("2026-09-18T12:00:0")
    installed(library, manifest=manifest("2"), code=OTHER)
    library.record_use("thing")
    assert library.active("thing")["uses"] == 1
    assert library.get(first["identity"])["uses"] == 1, "the withdrawn row keeps its own history"


def test_summary_has_exactly_the_contract_fields():
    """`docs/API_V2.md` names nine, the ninth being Teach Mode's `rule`. A tenth is a field the UI
    never asked for, so this stays an exact set rather than a subset check."""
    library = SkillLibrary()
    installed(library)
    assert set(library.summaries()[0]) == {
        "name", "purpose_plain", "version", "identity", "state", "learned_at", "last_used_at",
        "uses", "rule"}


def test_a_skill_the_build_asked_for_has_no_rule():
    """`null`, not `""`. A library screen has to tell "the owner taught me this" apart from "the
    build knew how to ask for this", and an empty string renders as a blank line under both."""
    library = SkillLibrary()
    installed(library)
    assert library.summaries()[0]["rule"] is None
    assert library.taught() == [], "a capability is not a taught rule"


def test_a_taught_rule_is_stored_and_comes_back_out():
    """The row carries the owner's sentence, and `taught()` is what a plan reads to find the rules
    to compose. A rule that lived only on the learning would be gone the moment it was approved."""
    from privatemesh.teach import taught_name

    rule = "never before 10am"
    library = SkillLibrary()
    manifest_ = manifest(name=taught_name(rule), rule=rule)
    library.install(manifest=manifest_, code=CODE, tests=TESTS, held_out=HELD_OUT)
    rows = library.taught()
    assert [r["rule"] for r in rows] == [rule]
    assert rows[0]["name"] == taught_name(rule)
    assert library.summaries()[0]["rule"] == rule
    library.revoke(taught_name(rule))
    assert library.taught() == [], "a revoked rule is not composed into a plan"


# ---------------------------------------------------------------- the bytes ------------------
def test_materialize_writes_the_module_and_not_the_tests(tmp_path):
    """The coder's tests must not land in the tree the coder's code can rewrite. Only the module
    is written; the held-out set is what grades it, and it never goes in here."""
    library = SkillLibrary()
    installed(library)
    row = library.active("thing")
    out = library.materialize(row, tmp_path / "skill")
    assert sorted(p.name for p in out.iterdir()) == ["thing.py"]


def test_the_library_survives_a_second_process_opening_the_same_file(tmp_path):
    """The compounding artifact: a later run, in a later process, gets the same bytes back. This is
    the whole reason the library is a file and not a dictionary."""
    path = tmp_path / "library.db"
    first = installed(SkillLibrary(path))
    reopened = SkillLibrary(path)
    assert reopened.active("thing")["identity"] == first["identity"]
    manifest_out, code, _tests, _held, _row = reopened.load("thing")
    assert code == CODE
    assert manifest_out.name == "thing"


def test_the_database_is_a_real_sqlite_file_not_a_pickle(tmp_path):
    """Stated as a test because "the owner's taught capabilities" is data the owner is entitled to
    read with a normal tool, not an opaque blob."""
    path = tmp_path / "library.db"
    installed(SkillLibrary(path))
    conn = sqlite3.connect(path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()
    assert "skills" in names
