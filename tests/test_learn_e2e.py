"""Concept D, over HTTP, in the assembled product.

Everything here goes through `docs/API_V2.md`: learn a capability from a real change, review what
the loop wrote, approve the exact identity on the screen, and then watch a LATER plan use it. The
one thing no test here does is reach into `learn.py` to make it succeed — a learning loop that only
works when a test drives its internals is not a product.

What is pinned, and why each one is worth a test:

* the whole flow: detect → learn → v1 stopped and rejected → v2 ready → approve → a later change
  uses the skill and skips learning;
* **identity**: a stale approval is a 409, and the good one installs exactly those bytes;
* **revocation**: a revoked skill is not used by the next plan;
* **the guard**: a skill that widens what fits compared with the owner's own rules is refused,
  recorded on the change, and the owner's rules still decide the plan.
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from privatemesh import wiring as wm
from privatemesh.api import default_app
from privatemesh.config import Settings
from privatemesh.learn import CAPABILITY, GENERATE_SYSTEM
from privatemesh.llm import TokenFactory
from privatemesh.ports import MailMessage
from privatemesh.sim import SAMPLE_DATE, build_sample_ports
from privatemesh.skillrt import SkillManifest

SETTINGS = Settings(api_key="sample-no-key", base_url="https://tf.test/v1/",
                    model_fast="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                    model_reason="nvidia/nemotron-3-super-120b-a12b",
                    model_deep="nvidia/Nemotron-3-Ultra-550b-a55b", tavily_key="")

HELD_OUT_DIR = Path(__file__).resolve().parent / "heldout" / CAPABILITY
DAY, PLAN_DAY = "2026-09-18", "2026-09-19"
WORKSHOP_ID, COMMITMENT_ID = "ev-alex-sync-2026-09-18", "protected-school-run-2026-09-18"


def _system_prompts(recorder: list[dict]) -> list[str]:
    """Every system message the provider was sent, in order.

    Read off the parsed request rather than grepped out of `json.dumps(recorder)`: a prompt is
    multi-line prose, and `json.dumps` escapes the newlines, so a substring match on the dump
    silently stops matching the moment a prompt gains a line — which is exactly what happened when
    the learn prompts started stating their JSON object."""
    return [str(m.get("content") or "")
            for body in recorder for m in (body.get("messages") or [])
            if m.get("role") == "system"]


def _recorded_model(recorder: list[dict]):
    def factory(gate):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            recorder.append(body)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": wm.sample_reply(body)}}],
                "usage": {"prompt_tokens": 700, "completion_tokens": 300, "total_tokens": 1000}})

        http = httpx.Client(base_url=SETTINGS.base_url, timeout=5,
                            transport=httpx.MockTransport(handler))
        return TokenFactory(SETTINGS, http, boundary=gate)

    return factory


@pytest.fixture
def data_dir(tmp_path) -> Path:
    """A real directory, so the library is a real file. The sample keeps everything in memory by
    default; the claim this file has to prove — a skill learned once is still there next time — is
    exactly the claim an in-memory library cannot demonstrate."""
    return tmp_path / "pm"


def build(data_dir: Path | None, recorder: list[dict], ports=None):
    return wm.build_sample(settings=SETTINGS, ports=ports or build_sample_ports(SAMPLE_DATE),
                           llm=_recorded_model(recorder), path=data_dir)


@pytest.fixture
def product(data_dir):
    """Watching, the way `privatemesh serve --v2 --sample` leaves it: the routine has already
    ticked, so the change the hero flow is about exists before the first request arrives.

    The ports come back too, because "teach it once" is a claim about the second email and the
    only way to make one arrive is to hand the simulator one.
    """
    recorder: list[dict] = []
    ports = build_sample_ports(SAMPLE_DATE)
    wired = build(data_dir, recorder, ports)
    wired.start()
    client = TestClient(default_app(SETTINGS, demo=True, v2=wired.service))
    yield wired, client, recorder, ports
    wired.stop()


# ---------------------------------------------------------------- helpers ----------------------
def _change_id(client, *, newest: bool = False) -> str:
    """The change the day is showing, oldest first — or the NEWEST one.

    `newest` exists because a second app instance over the same data directory now resumes the
    changes the first one made: they are persisted, so `today` legitimately lists yesterday's
    plan beside the fresh email. A test about the fresh email has to say so rather than take
    whichever change the store happens to list first.
    """
    for _ in range(200):
        today = client.get("/v2/today", params={"date": DAY}).json()
        if today["changes"]:
            rows = sorted(today["changes"], key=lambda c: c["received_at"])
            return (rows[-1] if newest else rows[0])["id"]
        time.sleep(0.01)
    raise AssertionError("the watcher never surfaced a change")


def _later_message(ports) -> MailMessage:
    """A second email from the same sender, an hour and a half later, asking for the same move.

    It has to be a real second MESSAGE rather than the same change prepared twice: the contract's
    reuse line is about "a later change that the library can serve", and the plan for one change
    is cached the moment it is prepared. Its day is derived the way the sample derives every day —
    the day after the newest message — so the two changes offer the same two slots.
    """
    first = ports.mail.get_thread("thread-alex")[0]
    return dataclasses.replace(
        first, id="msg-alex-cafe-2", rfc822_message_id="<alex-cafe-2@example.com>",
        received_at="2026-09-18T11:05:00.000Z", subject="Re: Our usual café has closed",
        body_text=("Sorry, second email — same thing. Could we meet near King's Cross tomorrow at "
                   "3 or 4 and you update the invite?\n\nAlex"))


def _await_second_change(client, first_id: str, *, tries: int = 400) -> str:
    for _ in range(tries):
        today = client.get("/v2/today", params={"date": DAY}).json()
        other = [c["id"] for c in today["changes"] if c["id"] != first_id]
        if other:
            return other[0]
        time.sleep(0.02)
    raise AssertionError("the second email never became a second change")


def _prepared(client, change_id: str | None = None) -> dict:
    change_id = change_id or _change_id(client)
    client.post(f"/v2/changes/{change_id}/prepare")
    return _await_change(client, change_id, "proposal_ready")


def _await_change(client, change_id: str, stage: str, *, tries: int = 2000) -> dict:
    body: dict = {}
    for _ in range(tries):
        body = client.get(f"/v2/changes/{change_id}").json()
        if body["stage"] == stage:
            return body
        time.sleep(0.02)
    raise AssertionError(f"stage {body.get('stage')!r} never reached {stage!r}")


def _await_learning(client, learning_id: str, *, tries: int = 4000) -> dict:
    """What the review screen does: poll until the loop settles. The contract is a stage machine,
    so the test drives it as one."""
    body: dict = {}
    for _ in range(tries):
        body = client.get(f"/v2/learning/{learning_id}").json()
        if body["stage"] in ("ready_for_review", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"learning stage {body.get('stage')!r} never settled")


def _learn(client, change_id: str) -> dict:
    started = client.post(f"/v2/changes/{change_id}/learn")
    assert started.status_code == 200, started.text
    return _await_learning(client, started.json()["learning_id"])


# ---------------------------------------------------------------- the headline flow ------------
def test_a_change_is_learned_approved_and_then_reused_by_a_later_plan(product):
    """The whole Concept D flow, once, through the API the web app calls.

    The reuse is the point. Teaching it once only means something if the second plan does not ask
    again — and the assertion that it did not is that `POST /learn` comes back 409.
    """
    wired, client, recorder, ports = product
    change_id = _change_id(client)
    prepared = _prepared(client)
    assert prepared["proposal"]["skill"] is None, "nothing has been learned yet"

    learning = _learn(client, change_id)
    assert learning["stage"] == "ready_for_review", learning
    assert learning["skill_name"] == CAPABILITY
    assert learning["purpose_plain"], "the owner is asked to approve a purpose, in plain words"
    assert [a["version"] for a in learning["attempts"]] == ["1.0.0", "1.0.1"]

    first, second = learning["attempts"]
    assert first["review"]["verdict"] == "reject"
    assert any(v["kind"] == "import" for v in first["violations"])
    assert second["held_out"] == {"passed": 20, "failed": 0}
    assert second["review"]["verdict"] == "approve"
    assert second["review"]["reviewer_model"] != SETTINGS.model_deep, (
        "the reviewer must not be the generator asked twice")

    # The manifest the owner reads, in their own words rather than in field names.
    manifest = learning["manifest"]
    assert manifest["identity"] == learning["attempts"][-1]["identity"]
    assert manifest["never"], "a manifest with nothing it may never do is not a consent screen"
    assert all(" " in item for item in manifest["reads"]), manifest["reads"]

    approved = client.post(f"/v2/learning/{learning['id']}/approve",
                           json={"identity": manifest["identity"]})
    assert approved.status_code == 200, approved.text

    # A LATER email arrives — a second change, not the same one prepared twice. Its plan uses the
    # skill, and the proposal says so in the owner's words.
    ports.mail.deliver(_later_message(ports))
    wired.tick()
    later_id = _await_second_change(client, change_id)
    assert later_id != change_id
    again = _prepared(client, later_id)
    assert again["proposal"]["skill"]["used"] is True, again["proposal"]["skill"]
    assert again["proposal"]["skill"]["identity"] == manifest["identity"]
    assert again["proposal"]["skill"]["reuse_line"].startswith("Using your skill: ")
    assert f"(learned {wired.library.active(CAPABILITY)['learned_at'][:10]})" in \
        again["proposal"]["skill"]["reuse_line"]

    why = client.get(f"/v2/changes/{later_id}/why").json()
    assert why["learned_skill"]["identity"] == manifest["identity"]
    assert why["overreach"] == []
    assert any("learned skill" in f"{u['label']} {u['detail']}".lower()
               for u in why["used"])

    # ...and the library now refuses to learn it a second time, rather than minting a new artifact
    # under the owner's old approval. This is the "skips learning" half of the reuse claim.
    refused = client.post(f"/v2/changes/{later_id}/learn")
    assert refused.status_code == 409, refused.text
    assert CAPABILITY in refused.json()["detail"]

    skills = client.get("/v2/skills").json()
    assert isinstance(skills, list) and len(skills) == 1
    assert skills[0]["name"] == CAPABILITY and skills[0]["state"] == "active"
    assert skills[0]["uses"] == 1, "the one plan that used it is the one that counted"
    assert skills[0]["last_used_at"], "a skill with no last-used time is one nobody can audit"

    # The loops' prompts went to the provider; the held-out cases never did.
    blob = json.dumps(recorder)
    assert any(p.startswith(GENERATE_SYSTEM) for p in _system_prompts(recorder))
    assert "the_canonical_1600_is_refused_by_the_buffer" not in blob


def test_the_demo_control_opens_the_later_request_on_the_same_day_as_the_first(product):
    """`POST /sample/next-change`: the button the web client shows once a change is finished.

    It is the only route a judge has to the second half of the claim, because a browser cannot
    reach into a Python object to deliver a second email, and it has to get two things right that
    are not about the delivery at all:

    * **The day.** The email says "tomorrow" and the planner reads the day off the message it was
      handed, so a message stamped with today's date asks for a day this sample has no calendar,
      no protected block and no research for. Stamped with the clock, the second change planned
      the 20th: a venue, a time and no boundary to respect, which is a different story from the
      one the first change tells.
    * **Which message is new.** Detection is cursor- and seen-driven; asking the engine for
      everything since the beginning classified the FIRST email a second time, so one message
      became two changes — and the one the owner was handed had never been planned.
    """
    wired, client, _recorder, _ports = product
    change_id = _change_id(client)
    prepared = _prepared(client)
    client.post(f"/v2/changes/{change_id}/approve", json={"version": prepared["version"]})
    _await_change(client, change_id, "done")

    opened = client.post("/sample/next-change")
    assert opened.status_code == 200, opened.text
    later_id = opened.json()["id"]
    assert later_id != change_id

    day = client.get("/v2/today", params={"date": PLAN_DAY}).json()
    # Exactly two, and the open one first: a third id here is the first email counted twice, and
    # the order is what a reload lands on — `changes[0]` is the thing the day screen offers to
    # handle.
    assert [(c["id"], c["stage"]) for c in day["changes"]] == [(later_id, "detected"),
                                                              (change_id, "done")]

    later = _prepared(client, later_id)
    assert later["proposal"]["start"][:10] == PLAN_DAY, later["proposal"]
    assert later["proposal"]["venue"]["name"] == "Fern & Finch Café"
    assert [r["name"] for r in later["proposal"]["rejected"]] == ["16:00–16:45",
                                                                 "Corner House Café"]
    # A second press while this one is still open is a refusal, not a third email: the control has
    # one more story in it, and the guard that says so is the same one the web fixture applies.
    refused = client.post("/sample/next-change")
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "invalid_stage"
    assert len(client.get("/v2/today", params={"date": PLAN_DAY}).json()["changes"]) == 2


def test_a_second_app_instance_reuses_the_skill_without_learning_anything(data_dir):
    """The compounding artifact, across processes: build, learn, approve, then throw the whole
    product away and build it again from the same directory. The skill is still there, and the
    second instance never asks a model to write it again.

    It is handed a FRESH email rather than the one the first instance handled: `routine.db` lives
    in the same directory and remembers what was already read, so the original message is not
    re-read as a new change — and a plan it never had to make would prove nothing about reuse.
    The original change is still THERE, though, because change records are persisted beside that
    cursor; the second instance resumes it rather than forgetting it. That is why everything below
    names the newest change explicitly instead of taking whichever one the day lists first.
    """
    recorder: list[dict] = []
    first = build(data_dir, recorder)
    first.start()
    client = TestClient(default_app(SETTINGS, demo=True, v2=first.service))
    change_id = _change_id(client)
    _prepared(client)
    learning = _learn(client, change_id)
    identity = learning["manifest"]["identity"]
    assert client.post(f"/v2/learning/{learning['id']}/approve",
                       json={"identity": identity}).status_code == 200
    first.stop()

    second_recorder: list[dict] = []
    second_ports = build_sample_ports(SAMPLE_DATE)
    second = build(data_dir, second_recorder, second_ports)
    second_ports.mail.deliver(_later_message(second_ports))
    second.start()
    try:
        assert second.library.active(CAPABILITY)["identity"] == identity
        again = TestClient(default_app(SETTINGS, demo=True, v2=second.service))
        assert [s["identity"] for s in again.get("/v2/skills").json()] == [identity]
        assert again.post(f"/v2/changes/{_change_id(again, newest=True)}/learn").status_code == 409
        prepared = _prepared(again, _change_id(again, newest=True))
        assert prepared["proposal"]["skill"]["identity"] == identity
        assert prepared["proposal"]["skill"]["used"] is True
        assert not any(p.startswith(GENERATE_SYSTEM)
                       for p in _system_prompts(second_recorder)), (
            "the second instance asked a model to write a skill it already had")
    finally:
        second.stop()


# ---------------------------------------------------------------- identity ----------------------
def test_approving_a_stale_identity_is_a_409_and_installs_nothing(product):
    wired, client, _recorder, _ports = product
    learning = _learn(client, _change_id(client))
    stale = "sha256:" + "0" * 64
    for identity in (stale, "", learning["manifest"]["identity"] + "0"):
        refused = client.post(f"/v2/learning/{learning['id']}/approve", json={"identity": identity})
        assert refused.status_code == 409, refused.text
    assert wired.library.serves(CAPABILITY) is False
    assert client.get("/v2/skills").json() == []


def test_rejecting_a_learning_leaves_the_library_empty(product):
    wired, client, _recorder, _ports = product
    learning = _learn(client, _change_id(client))
    assert client.post(f"/v2/learning/{learning['id']}/reject").status_code == 200
    assert client.get("/v2/skills").json() == []
    assert wired.library.serves(CAPABILITY) is False
    assert client.get(f"/v2/learning/{learning['id']}").json()["stage"] == "failed"
    assert client.get(f"/v2/learning/{learning['id']}/approve").status_code == 405


def test_learning_a_change_that_does_not_exist_is_a_404_and_spends_nothing():
    """`POST /learn` on an unknown change must not start a background thread against it, and must
    not have asked a model anything on the way to saying no."""
    recorder: list[dict] = []
    wired = build(None, recorder)
    try:
        client = TestClient(default_app(SETTINGS, demo=True, v2=wired.service))
        assert client.post("/v2/changes/nope/learn").status_code == 404
        assert client.get("/v2/learning/nope").status_code == 404
        assert recorder == [], "a 404 must not have spent a model call"
    finally:
        wired.stop()


# ---------------------------------------------------------------- revocation --------------------
def test_a_revoked_skill_is_not_used_by_the_next_plan(product):
    wired, client, _recorder, _ports = product
    learning = _learn(client, _change_id(client))
    client.post(f"/v2/learning/{learning['id']}/approve",
                json={"identity": learning["manifest"]["identity"]})
    assert client.get("/v2/skills").json()[0]["state"] == "active"

    revoked = client.post(f"/v2/skills/{CAPABILITY}/revoke")
    assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    assert client.get("/v2/skills").json()[0]["state"] == "revoked"
    assert wired.library.serves(CAPABILITY) is False

    after = _prepared(client)
    assert after["proposal"]["skill"] is None, (
        "a revoked skill must not shape a plan; the proposal should look as it did before "
        "anything was learned")
    assert after["proposal"]["overreach"] == [], (
        "a revoked skill was not refused, it was not consulted")
    # And running it is refused too, so nothing can reach the bytes by name.
    from privatemesh.library import LibraryError
    with pytest.raises(LibraryError, match="revoked"):
        wired.library.load(CAPABILITY)
    assert client.post(f"/v2/skills/{CAPABILITY}/revoke").status_code == 404


# ---------------------------------------------------------------- the guard ---------------------
#: A skill the owner could plausibly be shown and approve, that answers `fits: True` for every
#: slot. It is well-formed, so nothing but the cross-check against the owner's own rules can catch
#: it — which is the point: the runtime cannot see this, and trusting the generator is not a plan.
WIDENING_CODE = '''
def run(inputs):
    slots = list((inputs or {}).get("offered_slots") or ())
    return {"options": [{"start": s["start"], "end": s["end"], "fits": True, "code": "fits",
                         "reason": "%s-%s is free" % (s["start"][11:16], s["end"][11:16])}
                        for s in slots],
            "ranked": [{"start": s["start"], "end": s["end"], "venue": None} for s in slots]}
'''


def _install_widening(library) -> dict:
    from privatemesh.learn import INPUT_SCHEMA, OUTPUT_SCHEMA

    manifest = SkillManifest(name=CAPABILITY, version="9.9.9",
                             purpose="says everything fits",
                             inputs=INPUT_SCHEMA, outputs=OUTPUT_SCHEMA,
                             reads=tuple(sorted(INPUT_SCHEMA["properties"])), proposes=())
    return library.install(manifest=manifest, code=WIDENING_CODE,
                           purpose_plain="says every offered time fits")


def test_a_skill_that_widens_what_fits_is_refused_and_the_owners_rules_stand(product):
    """The asymmetry that makes generated code safe to let decide anything.

    `fit.check_slot` refuses 16:00 because of the protected buffer. The learned skill says it fits.
    The skill loses: the run is refused, the event lands on the change where the owner can see it,
    and the plan is the one the validator produced — NOT the one the skill asked for.
    """
    wired, client, _recorder, _ports = product
    _install_widening(wired.library)
    change_id = _change_id(client)
    prepared = _prepared(client)

    skill = prepared["proposal"]["skill"]
    assert skill["used"] is False, "a widening skill must not decide the plan"
    overreach = prepared["proposal"]["overreach"]
    assert len(overreach) == 1, overreach
    assert overreach[0]["skill"] == CAPABILITY
    assert overreach[0]["blocked_by"] == "manifest"
    assert "fits=True" in overreach[0]["detail"]
    assert overreach[0]["attempted"] == "widen what fits"

    # ...and the plan is the owner's rules, unchanged: 16:00 still rejected, 15:00 still chosen.
    rejected = {r["code"]: r for r in prepared["proposal"]["rejected"]}
    assert "overlaps_protected_buffer" in rejected, sorted(rejected)
    assert prepared["proposal"]["start"].endswith("15:00:00+01:00")

    why = client.get(f"/v2/changes/{change_id}/why").json()
    assert why["overreach"] == overreach, "the event must be on /why as well as on the proposal"
    outcome = client.get(f"/v2/changes/{change_id}/outcome").json()
    assert outcome["overreach"] == overreach

    # A refused run does not count as a use.
    assert wired.library.active(CAPABILITY)["uses"] == 0


def test_a_skill_that_cannot_run_is_reported_rather_than_silently_skipped(product):
    """The other failure path: the code is well-formed, the manifest is well-formed, and the
    RUNTIME stops it. `blocked_by` says `runtime`, because that is a different fact about the
    generated code than "it answered wrongly"."""
    wired, client, _recorder, _ports = product
    from privatemesh.learn import INPUT_SCHEMA, OUTPUT_SCHEMA

    code = "import subprocess\n\n\ndef run(inputs):\n    return {'options': [], 'ranked': []}\n"
    manifest = SkillManifest(name=CAPABILITY, version="9.9.9", purpose="escapes",
                             inputs=INPUT_SCHEMA, outputs=OUTPUT_SCHEMA,
                             reads=tuple(sorted(INPUT_SCHEMA["properties"])), proposes=())
    wired.library.install(manifest=manifest, code=code, purpose_plain="escapes")
    prepared = _prepared(client)
    assert prepared["proposal"]["skill"]["used"] is False
    assert prepared["proposal"]["overreach"][0]["blocked_by"] == "runtime"
    assert "subprocess" in prepared["proposal"]["overreach"][0]["detail"]
    assert prepared["proposal"]["start"].endswith("15:00:00+01:00")
