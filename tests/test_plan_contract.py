from privatemesh.planner import build_plan_prompt


class _T:
    name = "send_message"
    description = "Send a message"
    parameters = {"type": "object", "required": ["to", "body"],
                  "properties": {"to": {"type": "string"}, "body": {"type": "string"},
                                 "cc": {"type": "string"}}}


def test_the_plan_prompt_names_every_argument_and_which_are_required():
    p = build_plan_prompt("tell dana", [_T()])
    assert "to: string" in p and "body: string" in p and "cc?: string" in p


def test_a_call_missing_required_arguments_is_refused_before_any_approval(broker, conn):
    inv = broker.invoke("t-args", "send_message", {})
    assert inv.status != "needs_approval"
    assert "to" in (inv.error or "") and "body" in (inv.error or "")
    assert broker.approvals.pending() == [] if hasattr(broker.approvals, "pending") else True
