"""Exporting taught skills as Hermes `SKILL.md` files (NemoClaw's persistence format).

Two refusals are the whole safety story, and both are pinned here against an artifact this file
builds by hand rather than through the full learning loop — which is the point: `SkillLibrary.install`
enforces identity, not behaviour, so a row with a non-empty `rule` column proves nothing about
whether the code behind it still narrows. The exporter has to check the bytes, not the column.

* **Narrowing-only is re-verified, not trusted.** A skill installed directly with code that keeps
  everything regardless of the trusted rules is refused at export, even though its `rule` column
  says "taught" exactly like a correct one's does.
* **The privacy gate is the same one egress uses.** A rule that quotes a protected value verbatim
  is refused before any bytes reach disk.

Also pinned: determinism (two exports of one library are byte-identical), and that only ACTIVE
taught skills are ever considered — never the one hard-coded capability, never a revoked version.
"""
from __future__ import annotations

import pytest

from privatemesh import boundary as boundary_module
from privatemesh.boundary import BoundaryStore, EgressRefused, PrivacyGate
from privatemesh.export import ExportRefused, export_taught_skills, verify_narrowing_only
from privatemesh.learn import CAPABILITY, CAPABILITY_PURPOSE, INPUT_SCHEMA, OUTPUT_SCHEMA
from privatemesh.library import SkillLibrary
from privatemesh.skillrt import SkillManifest
from privatemesh.teach import taught_name

RULE = "never before 10am"
READS = tuple(sorted(INPUT_SCHEMA["properties"]))

#: A correct taught skill: the full trusted evaluator, plus the one narrowing the rule names. Every
#: case `teach.invariant_cases()` offers is answered by re-deriving the base verdict and then
#: narrowing it — never widening it — exactly as a generated taught skill is required to.
NARROWS_CODE = '''
import datetime as _dt


def _when(v):
    return _dt.datetime.fromisoformat(str(v))


def _hhmm(m):
    return m.strftime("%H:%M")


def _base(offered, protected, busy, day_start, day_end, now, duration, notice):
    start = _when(offered["start"])
    end = _when(offered["end"])
    if end <= start:
        return start, end, False, "empty_slot", "not a meeting"
    if now is not None and end <= now:
        return start, end, False, "in_the_past", "already passed"
    if day_start is not None and start < day_start:
        return start, end, False, "outside_day", "before the day starts"
    if day_end is not None and end > day_end:
        return start, end, False, "outside_day", "after the day ends"
    if protected is not None:
        p_start, p_end, buffer_minutes = protected
        buffer_start = p_start - _dt.timedelta(minutes=buffer_minutes)
        if start < p_start and end > buffer_start:
            return start, end, False, "overlaps_protected_buffer", "runs into the buffer"
        if start < p_end and end > p_start:
            return start, end, False, "overlaps_protected", "runs into the protected commitment"
    for window in busy or ():
        b_start = _when(window["start"])
        b_end = _when(window["end"])
        if start < b_end and end > b_start:
            return start, end, False, "overlaps_busy", "overlaps something booked"
    minutes = int((end - start).total_seconds() // 60)
    if duration > 0 and minutes != duration:
        return start, end, False, "wrong_duration", "wrong length"
    if notice > 0 and now is not None and start < now + _dt.timedelta(minutes=notice):
        return start, end, False, "too_soon", "not enough notice"
    return start, end, True, "fits", "free"


def run(inputs):
    inputs = inputs or {}
    duration = int(inputs.get("duration_minutes") or 0)
    notice = int(inputs.get("minimum_notice_minutes") or 0)
    now = _when(inputs["now"]) if inputs.get("now") else None
    day_start = _when(inputs["day_start"]) if inputs.get("day_start") else None
    day_end = _when(inputs["day_end"]) if inputs.get("day_end") else None
    protected = None
    fact = inputs.get("protected")
    if isinstance(fact, dict) and fact.get("start") and fact.get("end"):
        protected = (_when(fact["start"]), _when(fact["end"]),
                     max(0, int(fact.get("buffer_minutes") or 0)))
    options = []
    for offered in inputs.get("offered_slots") or ():
        start, end, fits, code, reason = _base(offered, protected, inputs.get("busy"), day_start,
                                                day_end, now, duration, notice)
        if fits and start.hour < 10:
            fits, code, reason = False, "taught_rule_never_before_10am", "before 10am"
        options.append({"start": start.isoformat(), "end": end.isoformat(), "fits": fits,
                        "code": code, "reason": reason})
    ranked = [{"start": o["start"], "end": o["end"], "venue": None}
              for o in sorted((o for o in options if o["fits"]), key=lambda o: o["start"])]
    return {"options": options, "ranked": ranked}
'''

#: A skill whose `rule` column says "taught" but whose code keeps every offered slot regardless —
#: the exact shape a compromised or mis-graded row would have. `SkillLibrary.install` has no way to
#: refuse this: it enforces identity (the hash matches its own bytes), never behaviour.
WIDENS_CODE = '''
def run(inputs):
    slots = list((inputs or {}).get("offered_slots") or ())
    return {"options": [{"start": s["start"], "end": s["end"], "fits": True, "code": "fits",
                         "reason": "kept"} for s in slots],
            "ranked": [{"start": s["start"], "end": s["end"], "venue": None} for s in slots]}
'''

TESTS_CODE = "def test_smoke():\n    pass\n"


def _manifest(rule: str, *, name: str | None = None, version: str = "1.0.0") -> SkillManifest:
    return SkillManifest(name=name or taught_name(rule), version=version,
                         purpose=f'Apply the rule the owner taught — "{rule}" — as a narrowing '
                                 f"filter; pure function, proposes nothing.",
                         module="taught_rule", entry="run", inputs=INPUT_SCHEMA,
                         outputs=OUTPUT_SCHEMA, reads=READS, proposes=(), rule=rule)


def _install(library: SkillLibrary, *, rule: str = RULE, code: str = NARROWS_CODE,
            name: str | None = None, version: str = "1.0.0") -> dict:
    return library.install(manifest=_manifest(rule, name=name, version=version), code=code,
                           tests=TESTS_CODE, held_out="",
                           purpose_plain=f"Applies your rule: {rule}")


@pytest.fixture
def boundary():
    return BoundaryStore()


@pytest.fixture
def gate(boundary):
    return PrivacyGate(boundary)


# ---------------------------------------------------------------- narrowing-only ----------------
def test_a_correct_taught_skill_is_verified_and_exported(tmp_path, gate):
    library = SkillLibrary()
    _install(library)
    exported = export_taught_skills(library, tmp_path, gate)
    assert len(exported) == 1
    assert exported[0].path == tmp_path / exported[0].name / "SKILL.md"
    assert exported[0].path.read_text(encoding="utf-8") == exported[0].content


def test_a_skill_whose_rule_column_says_taught_but_whose_code_widens_is_refused(tmp_path, gate):
    """The row looks exactly like a correct taught skill from the outside — non-empty `rule`,
    installed through the same `SkillLibrary.install` — and the exporter must not take that at
    face value. This is the test for "verify the property rather than trusting the flag"."""
    library = SkillLibrary()
    _install(library, code=WIDENS_CODE)
    with pytest.raises(ExportRefused, match="not narrowing-only"):
        export_taught_skills(library, tmp_path, gate)
    assert not any(tmp_path.iterdir()), "a refused export must not leave a partial file behind"


def test_verify_narrowing_only_runs_the_code_not_the_flag():
    manifest = _manifest(RULE)
    verify_narrowing_only(manifest, NARROWS_CODE)              # does not raise
    with pytest.raises(ExportRefused, match="not narrowing-only"):
        verify_narrowing_only(manifest, WIDENS_CODE)


def test_a_skill_with_no_rule_at_all_is_refused_even_if_asked_for_directly():
    manifest = SkillManifest(name=CAPABILITY, version="1.0.0", purpose=CAPABILITY_PURPOSE,
                             module=CAPABILITY, entry="run", inputs=INPUT_SCHEMA,
                             outputs=OUTPUT_SCHEMA, reads=READS, proposes=(), rule="")
    with pytest.raises(ExportRefused, match="no taught rule"):
        verify_narrowing_only(manifest, NARROWS_CODE)


def test_the_hard_coded_capability_is_never_exported_even_when_present_in_the_library(tmp_path,
                                                                                       gate):
    library = SkillLibrary()
    library.install(manifest=SkillManifest(name=CAPABILITY, version="1.0.0",
                                           purpose=CAPABILITY_PURPOSE, module=CAPABILITY,
                                           entry="run", inputs=INPUT_SCHEMA, outputs=OUTPUT_SCHEMA,
                                           reads=READS, proposes=(), rule=""),
                    code=NARROWS_CODE, tests=TESTS_CODE, purpose_plain=CAPABILITY_PURPOSE)
    _install(library)
    exported = export_taught_skills(library, tmp_path, gate)
    assert [e.name for e in exported] == [taught_name(RULE)]


def test_a_revoked_version_is_not_exported_only_the_live_one_is(tmp_path, gate):
    library = SkillLibrary()
    name = taught_name(RULE)
    _install(library, name=name, version="1.0.0")
    _install(library, name=name, version="1.0.1")
    exported = export_taught_skills(library, tmp_path, gate)
    assert len(exported) == 1
    assert '"1.0.1"' in exported[0].content
    assert '"1.0.0"' not in exported[0].content


# ---------------------------------------------------------------- the privacy gate ---------------
MARKER = "MARKER_RULE_TITLE_Ada_piano_recital_at_the_conservatory"


def test_the_gate_is_required_not_an_opt_in(tmp_path):
    """Review finding, Medium: `gate` used to default to `None` and skip the check entirely, so a
    direct or library caller who simply forgot the argument got an unchecked export — the CLI path
    happened to always pass one, which hid the gap. `gate` is now a required positional, so leaving
    it out is a `TypeError` before a single skill is even loaded, not a silent narrower guarantee."""
    library = SkillLibrary()
    _install(library)
    with pytest.raises(TypeError):
        export_taught_skills(library, tmp_path)  # type: ignore[call-arg]
    assert not any(tmp_path.iterdir()), "no file may be written on a call missing the gate"


def test_a_rule_that_quotes_a_protected_value_is_refused_before_anything_is_written(tmp_path,
                                                                                    boundary, gate):
    boundary.put(id="pc1", start="2026-09-19T16:30:00+01:00", end="2026-09-19T17:30:00+01:00",
                buffer_minutes=45, title=MARKER)
    library = SkillLibrary()
    _install(library, rule=f"nothing that mentions {MARKER}")
    with pytest.raises(EgressRefused):
        export_taught_skills(library, tmp_path, gate)
    assert not any(tmp_path.iterdir()), "a refused export must not leave a partial file behind"


def test_a_rule_with_no_protected_overlap_passes_the_same_gate(tmp_path, gate):
    library = SkillLibrary()
    _install(library)
    exported = export_taught_skills(library, tmp_path, gate)
    assert len(exported) == 1


# ---------------------------------------------------------------- determinism --------------------
def test_two_exports_of_one_library_are_byte_identical(tmp_path, gate):
    library = SkillLibrary()
    _install(library)
    first = export_taught_skills(library, tmp_path / "a", gate)
    second = export_taught_skills(library, tmp_path / "b", gate)
    assert first[0].content == second[0].content
    assert (tmp_path / "a" / first[0].name / "SKILL.md").read_bytes() == (
        tmp_path / "b" / second[0].name / "SKILL.md").read_bytes()


def test_the_export_carries_the_identity_and_the_rule_the_library_already_recorded(tmp_path, gate):
    library = SkillLibrary()
    row = _install(library)
    exported = export_taught_skills(library, tmp_path, gate)[0]
    assert row["identity"] in exported.content
    assert RULE in exported.content
    assert exported.name == row["name"]


def test_the_frontmatter_states_what_the_rule_may_and_may_not_do(tmp_path, gate):
    library = SkillLibrary()
    _install(library)
    content = export_taught_skills(library, tmp_path, gate)[0].content
    assert content.startswith("---\n")
    front, _, body = content[4:].partition("\n---\n")
    assert "narrowing_only: true" in front
    assert "may:" in front and "may_not:" in front
    assert "may never" in body.lower() or "may not" in body.lower()
