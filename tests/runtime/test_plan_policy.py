from nanobot.runtime.plan_policy import (
    PlanPolicyKind,
    PlanPolicyState,
    complex_request_reason,
    decide_plan_policy,
)


def test_single_trivial_read_can_run_without_plan() -> None:
    decision = decide_plan_policy(["web_search"], PlanPolicyState())

    assert decision.kind is PlanPolicyKind.OPTIONAL


def test_second_read_promotes_turn_to_required_plan() -> None:
    state = PlanPolicyState(business_tool_calls=1)

    decision = decide_plan_policy(["web_fetch"], state)

    assert decision.kind is PlanPolicyKind.REQUIRED
    assert decision.reason == "runtime_promoted_after_first_read"


def test_multiple_business_tools_require_plan() -> None:
    decision = decide_plan_policy(
        ["web_search", "web_fetch"],
        PlanPolicyState(),
    )

    assert decision.kind is PlanPolicyKind.REQUIRED


def test_mutating_or_long_running_tool_requires_plan() -> None:
    for name in ("write_file", "exec", "spawn_subagent", "create_pdf"):
        assert decide_plan_policy(
            [name],
            PlanPolicyState(),
        ).kind is PlanPolicyKind.REQUIRED


def test_existing_dynamic_plan_opens_barrier() -> None:
    state = PlanPolicyState(plan_created=True)

    assert decide_plan_policy(["write_file"], state).kind is PlanPolicyKind.OPTIONAL


def test_expert_team_uses_runtime_workflow_plan() -> None:
    decision = decide_plan_policy(
        ["spawn_subagent", "write_file"],
        PlanPolicyState(),
        expert_team=True,
    )

    assert decision.kind is PlanPolicyKind.WORKFLOW


def test_explicit_research_intent_requires_plan_from_first_read() -> None:
    state = PlanPolicyState(
        forced_reason=complex_request_reason("帮我分析下比亚迪 A 股"),
    )

    decision = decide_plan_policy(["web_search"], state)

    assert decision.kind is PlanPolicyKind.REQUIRED
    assert decision.reason == "explicit_complex_request"


def test_simple_explanation_does_not_force_plan() -> None:
    assert complex_request_reason("什么是自由现金流？") is None
