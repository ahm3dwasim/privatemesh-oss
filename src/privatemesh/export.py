"""Export taught skills as Hermes Agent `SKILL.md` files — NemoClaw's own persistence format for a
learned capability that must survive redeployment.

Read to get the shape right, and named so the claim below is checkable rather than taken on faith:
`https://github.com/NVIDIA/NemoClaw` — `.agents/skills/nemoclaw-skills-guide/SKILL.md` (frontmatter
keys `name`, `description`, `license`; a Markdown body) — and `https://github.com/NVIDIA/OpenShell`
for the sandbox this file is meant to be read inside of. What NemoClaw documents is exactly those
three frontmatter keys and the fact that a `SKILL.md` is one file per skill; everything else this
module emits — the `privatemesh:` frontmatter block, the "what this may/may not do" sections, the
verification paragraph — is OUR convention, layered on top and labelled as such in
`docs/NVIDIA_STACK.md`. This module does not claim NemoClaw specifies any of that.

Two refusals happen before a single byte is written, and both are checks on the ARTIFACT rather
than on a flag that describes it:

* **Narrowing-only, re-verified.** A row's `rule` column being non-empty means the library calls it
  "taught"; it does not mean the code behind it still narrows. `verify_narrowing_only` does not
  trust that column — it re-runs the stored code, in the same isolated runtime the learning loop
  graded it in, over PrivateMesh's fixed safety cases (`teach.invariant_cases`), and refuses unless
  `teach.widenings` comes back empty on every one. That is the same invariant `learn._grade_taught`
  checked once at install time, checked again, independently, at the moment the artifact is about
  to leave the process.
* **No protected value, and no owner datum.** The rendered file is run through the same
  `PrivacyGate.check_text` every outbound model payload passes through (`llm.TokenFactory.chat`),
  before it touches disk. A rule that happens to quote a protected fact, or a purpose sentence that
  paraphrases one, is refused exactly as an outbound email would be — one gate, one behaviour,
  whichever surface is leaving the process.

Nothing here reaches into the library's other capability (`learn.CAPABILITY`, the one the build
knows how to ask for): its `rule` is `""`, `SkillLibrary.taught()` never returns it, and this module
never asks for it by name. Only what the owner taught is exportable.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .boundary import PrivacyGate
from .learn import PLAIN_NEVER, PLAIN_READS, RULE_ORDER
from .library import SkillLibrary
from .skillrt import OK, LocalIsolatedRunner, RunSpec
from .skillrt.manifest import SkillManifest
from .teach import invariant_cases, widenings

#: Ours. NemoClaw's own `SKILL.md`s (see the module docstring) use exactly these three top-level
#: frontmatter keys; everything nested under `FRONTMATTER_EXTENSION_KEY` is PrivateMesh's own
#: addition and is documented as such rather than attributed to NemoClaw.
FRONTMATTER_EXTENSION_KEY = "privatemesh"

#: The rules a taught skill is judged against BEFORE the owner's sentence ever applies — see
#: `learn.RULE_ORDER`. Named in the doc body so a reader of the exported file, not only a reader of
#: this repository, can see what "the trusted rules" refers to.
_RULE_ORDER_PROSE = ", ".join(RULE_ORDER)


class ExportRefused(RuntimeError):
    """One taught skill could not be exported as the artifact it claims to be."""


@dataclass(frozen=True)
class ExportedSkill:
    """One written file, and the identity it was rendered from."""

    name: str
    identity: str
    path: Path
    content: str


def verify_narrowing_only(manifest: SkillManifest, code: str, *, runner=None) -> None:
    """Re-run `code` over the fixed safety cases and refuse unless it never widens.

    This is the independent check the module docstring promises: it does not read `manifest.rule`
    and stop there, and it does not trust that the row came from `SkillLibrary.taught()` — it
    materialises the exact bytes, runs them in the same isolated runtime the learning loop uses, and
    asks the same question `learn._grade_taught` asked at install time. A skill that answers
    differently now than it did then (or that was written directly into the library, bypassing the
    loop entirely — the library's `install` enforces identity, not behaviour) is caught here.
    """
    if not manifest.rule:
        raise ExportRefused(
            f"'{manifest.name}' has no taught rule; only a taught, narrowing-only skill is "
            f"exportable as a Hermes SKILL.md")
    runner = runner or LocalIsolatedRunner()
    root = Path(tempfile.mkdtemp(prefix="pmexport-verify-"))
    try:
        skill_dir = root / "skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / f"{manifest.module_name}.py").write_text(code, encoding="utf-8")
        for i, case in enumerate(invariant_cases()):
            result = runner.run(RunSpec(skill_dir=skill_dir, module=manifest.module_name,
                                        entry=manifest.entry, inputs=case))
            if result.status != OK:
                detail = (result.violation.detail if result.violation is not None
                          else (result.error or f"the runtime returned {result.status}"))
                raise ExportRefused(
                    f"'{manifest.name}' could not be re-verified as narrowing-only: case {i} "
                    f"stopped the runtime ({detail})")
            try:
                answer = manifest.validate_result(result.result)
            except Exception as exc:                                    # noqa: BLE001
                raise ExportRefused(
                    f"'{manifest.name}' could not be re-verified as narrowing-only: case {i}'s "
                    f"answer does not match its own declared outputs ({exc})") from exc
            problems = widenings(case, answer)
            if problems:
                raise ExportRefused(
                    f"'{manifest.name}' is not narrowing-only: case {i} — " + "; ".join(problems))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def render_skill_md(manifest: SkillManifest, row: dict) -> str:
    """The bytes of one `SKILL.md`. Pure: same `(manifest, row)` in, same string out — the property
    `export_taught_skills`'s determinism test exercises."""
    reads = sorted(manifest.reads)
    never = list(PLAIN_NEVER)
    front = _frontmatter(manifest, row, reads=reads, never=never)
    body = _body(manifest, row, reads=reads, never=never)
    return front + body


def _yaml_str(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_list(items, indent: str = "  ") -> str:
    if not items:
        return f"{indent}[]"
    return "\n".join(f"{indent}- {_yaml_str(item)}" for item in items)


def _frontmatter(manifest: SkillManifest, row: dict, *, reads: list[str], never: list[str]) -> str:
    lines = [
        "---",
        f"name: {_yaml_str(row['name'])}",
        f"description: {_yaml_str(manifest.purpose)}",
        "license: \"Apache-2.0\"",
        f"{FRONTMATTER_EXTENSION_KEY}:",
        f"  identity: {_yaml_str(row['identity'])}",
        f"  version: {_yaml_str(manifest.version)}",
        f"  rule: {_yaml_str(manifest.rule)}",
        f"  expires: {_yaml_str(manifest.expires or 'never')}",
        "  narrowing_only: true",
        "  reads:",
        _yaml_list(reads, "    "),
        "  proposes:",
        _yaml_list(list(manifest.proposes), "    "),
        "  may:",
        _yaml_list(
            [f"remove an offered slot the trusted rules ({_RULE_ORDER_PROSE}) already kept, "
             f"exactly where the rule above requires it"], "    "),
        "  may_not:",
        _yaml_list(
            ["add, move or invent a slot nobody offered",
             "keep (fits=true) a slot the trusted rules above already refuse",
             *never], "    "),
        "---",
        "",
    ]
    return "\n".join(lines) + "\n"


def _body(manifest: SkillManifest, row: dict, *, reads: list[str], never: list[str]) -> str:
    plain_reads = [PLAIN_READS.get(f, f) for f in reads]
    lines = [
        f"# {row['name']}",
        "",
        f'**Owner\'s rule:** "{manifest.rule}"',
        "",
        manifest.purpose,
        "",
        "## What this may do",
        "",
        f"Narrow only: remove an offered meeting slot that PrivateMesh's own trusted rules "
        f"({_RULE_ORDER_PROSE}) already kept, wherever the rule above requires it.",
        "",
        "## What this may never do",
        "",
        "- Add, move or invent a time slot nobody offered.",
        "- Keep (`fits=true`) a slot the trusted rules above already refuse.",
        *[f"- {item}" for item in never],
        "",
        "## What it reads",
        "",
        *[f"- {item}" for item in plain_reads],
        "",
        "## Verification",
        "",
        f"- Identity: `{row['identity']}` — the sha256 of its code, its manifest and its tests; "
        f"re-derived from the stored bytes on every load and refused on any mismatch "
        f"(`SkillLibrary.load`).",
        "- Narrowing-only: re-verified at EXPORT time, independent of the `rule` column on this "
        "row, by re-running this exact code over PrivateMesh's fixed safety cases "
        "(`teach.invariant_cases`) and checking it never keeps a slot the trusted rules refuse "
        "(`teach.widenings`). See `docs/NVIDIA_STACK.md`.",
        f"- Learned: {row.get('learned_at') or 'unknown'}. Expires: {manifest.expires or 'never'}.",
        "",
        "## Examples",
        "",
        "The owner approved this rule against labelled examples on the approval screen "
        "(`POST /v2/teach`). PrivateMesh's library does not persist those examples once a skill is "
        "installed, so none are reproduced here — only the rule, the code's identity and this "
        "file's own re-verification are.",
        "",
    ]
    return "\n".join(lines)


def export_taught_skills(library: SkillLibrary, out_dir: str | Path, gate: PrivacyGate, *,
                          runner=None) -> list[ExportedSkill]:
    """Write one `<name>/SKILL.md` per ACTIVE taught skill in `library`, into `out_dir`.

    `gate` is required, not an opt-in: the module's own docstring claims every exported file is run
    through the same `PrivacyGate.check_text` every outbound model payload passes through, and a
    default that let a caller skip it would make that claim false for whichever caller took the
    default. Both checks — narrowing-only, and the privacy gate — run for EVERY row before anything
    is written, so a library that fails on its second skill leaves `out_dir` exactly as it was: no
    partial export, and no file on disk the two refusals above did not clear.
    """
    out_dir = Path(out_dir)
    runner = runner or LocalIsolatedRunner()
    rendered: list[tuple[dict, str]] = []
    for summary in library.taught():
        manifest, code, _tests, _held_out, row = library.load(summary["identity"])
        verify_narrowing_only(manifest, code, runner=runner)
        content = render_skill_md(manifest, row)
        gate.check_text(content, kind="model", field="skill_export", artifact=row["identity"])
        rendered.append((row, content))
    out_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    for row, content in rendered:
        skill_dir = out_dir / row["name"]
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
        path.write_text(content, encoding="utf-8")
        exported.append(ExportedSkill(name=row["name"], identity=row["identity"], path=path,
                                      content=content))
    return exported
