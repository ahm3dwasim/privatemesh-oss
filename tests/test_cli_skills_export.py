"""`privatemesh skills export --format hermes --out DIR` — the CLI surface over `export.py`.

The library and the boundary this reads are the same data directory `serve --v2 --live` uses
(`data/v2`, relative to the cwd), so every test here `chdir`s into a fresh `tmp_path` first —
exactly the guard `test_serve_v2_sample_starts_with_no_credentials...` already uses, and for the
same reason: this must never touch the repository's own `data/v2` if one exists.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from privatemesh.cli import main
from privatemesh.learn import INPUT_SCHEMA, OUTPUT_SCHEMA
from privatemesh.library import SkillLibrary
from privatemesh.skillrt import SkillManifest
from privatemesh.teach import taught_name

RULE = "never before 10am"
READS = tuple(sorted(INPUT_SCHEMA["properties"]))
#: The full trusted evaluator (same shape as `sample_skill_code.V2_MODULE`), plus the one narrowing
#: rule named above. A skill that only checked its own rule and ignored the base ones — busy,
#: protected, the working day — would be refused by the exporter's re-verification, which is
#: exactly what this fixture must NOT be, so it does the whole job rather than a shortcut of it.
CODE = '''
import datetime as _dt


def _when(v):
    return _dt.datetime.fromisoformat(str(v))


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
TESTS_CODE = "def test_smoke():\n    pass\n"


def _seed_library(data_dir: Path) -> None:
    library = SkillLibrary(str(data_dir / "library.db"))
    manifest = SkillManifest(name=taught_name(RULE), version="1.0.0",
                             purpose=f'Apply the rule the owner taught — "{RULE}".',
                             module="taught_rule", entry="run", inputs=INPUT_SCHEMA,
                             outputs=OUTPUT_SCHEMA, reads=READS, proposes=(), rule=RULE)
    library.install(manifest=manifest, code=CODE, tests=TESTS_CODE, held_out="",
                    purpose_plain=f"Applies your rule: {RULE}")


def test_skills_export_writes_a_skill_md_per_taught_skill(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    _seed_library(tmp_path / "data" / "v2")
    rc = main(["skills", "export", "--format", "hermes", "--out", "out/hermes"])
    assert rc == 0
    written = tmp_path / "out" / "hermes" / taught_name(RULE) / "SKILL.md"
    assert written.exists()
    assert "narrowing_only: true" in written.read_text(encoding="utf-8")
    assert "wrote" in capsys.readouterr().out


def test_skills_export_with_an_empty_library_writes_nothing_and_exits_zero(monkeypatch, tmp_path,
                                                                           capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["skills", "export", "--format", "hermes", "--out", "out/hermes"])
    assert rc == 0
    assert "no taught skills" in capsys.readouterr().out


def test_skills_export_rejects_an_unsupported_format(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["skills", "export", "--format", "openai", "--out", "out"])
    assert rc == 2
    assert "unsupported --format" in capsys.readouterr().err


def test_skills_export_requires_out(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["skills", "export", "--format", "hermes"])
    assert rc == 2
    assert "--out DIR is required" in capsys.readouterr().err


def test_skills_with_no_subcommand_prints_usage(capsys):
    rc = main(["skills"])
    assert rc == 2
    assert "skills export" in capsys.readouterr().err
