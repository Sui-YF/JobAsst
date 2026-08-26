from __future__ import annotations

from datetime import datetime

import database
from application_workflow import determine_next_action, transition
from deepseek_client import (
    analyze_jd, generate_polish_strategy, propose_resume_edits, stabilize_incremental_analysis,
)
from resume_editor import validate_and_apply_edits
from unified_profile import build_experience_blocks, canonical_evidence_pool, resolved_fact_controls


def clarification_candidates(analysis: dict, limit: int = 3) -> list[dict]:
    candidates = []
    for requirement in analysis.get("requirements", []):
        if requirement.get("nature") != "match":
            continue
        if requirement.get("gap_type") != "expression_gap":
            continue
        if requirement.get("match_status") not in {"no_evidence", "needs_confirmation", "direct_partial", "transferable_match"}:
            continue
        weight = int(requirement.get("weight", 1))
        impact = weight * 10 + (3 if requirement.get("fact_ids") else 0)
        candidates.append((impact, requirement))
    return [item for _, item in sorted(candidates, key=lambda pair: pair[0], reverse=True)[:max(1, min(limit, 3))]]


def analyze_application(user_id: str, application_id: str) -> dict:
    workspace = database.get_application_workspace(user_id, application_id)
    if workspace["application_status"] in {"JD_READY", "NEEDS_REMATCH"}:
        transition(user_id, application_id, "ANALYZING")
    elif workspace["application_status"] != "ANALYZING":
        raise RuntimeError("当前状态不能执行岗位分析")
    facts = canonical_evidence_pool(database.list_facts(user_id, confirmed_only=True))
    analysis = analyze_jd(workspace["jd_text"], facts, user_id=user_id)
    target = "NEEDS_CLARIFICATION" if clarification_candidates(analysis) else "READY_FOR_STRATEGY"
    database.update_application_fields(
        user_id, application_id, analysis_json=analysis,
        requirement_state_json={"requirements": analysis.get("requirements", [])},
        eligibility_state_json={"requirements": [r for r in analysis.get("requirements", []) if r.get("nature") == "eligibility"]},
        evidence_revision=database.get_user(user_id)["career_revision"],
    )
    transition(user_id, application_id, target)
    return database.get_application_workspace(user_id, application_id)


def skip_clarification(user_id: str, application_id: str) -> dict:
    return transition(user_id, application_id, "READY_FOR_STRATEGY")


def confirm_experience_case(user_id: str, application_id: str, case: dict, evidence_items: list[dict]) -> dict:
    workspace = database.get_application_workspace(user_id, application_id)
    if workspace["application_status"] != "NEEDS_CLARIFICATION":
        raise RuntimeError("当前Application不等待经历确认")
    case_id = database.add_experience_case(user_id, case)
    for evidence in evidence_items:
        evidence_type = evidence.get("evidence_type", "Other")
        database.add_or_merge_user_confirmed_fact(user_id, {
            **evidence, "case_id": case_id, "context": case.get("summary", ""),
            "organization": evidence.get("organization", "") if evidence_type == "Employment" else "",
            "official_job_title": evidence.get("official_job_title", "") if evidence_type == "Employment" else "",
            "verification_status": "confirmed", "evidence_origin": "user_confirmed",
            "source": f"用户确认经历：{case_id}",
        })
    database.update_application_fields(user_id, application_id, deep_dive_state_json={
        "confirmed_case_id": case_id, "confirmed_at": datetime.now().isoformat(timespec="seconds")
    })
    return rematch_application(user_id, application_id)


def rematch_application(user_id: str, application_id: str) -> dict:
    workspace = database.get_application_workspace(user_id, application_id)
    before = workspace.get("analysis") or {}
    if workspace["application_status"] != "ANALYZING":
        if workspace["application_status"] not in {"NEEDS_CLARIFICATION", "NEEDS_REMATCH"}:
            raise RuntimeError("当前状态不能重新匹配")
        transition(user_id, application_id, "ANALYZING")
    facts = canonical_evidence_pool(database.list_facts(user_id, confirmed_only=True))
    refreshed = analyze_jd(workspace["jd_text"], facts, user_id=user_id, operation="REMATCH")
    refreshed = stabilize_incremental_analysis(before, refreshed, facts)
    database.update_application_fields(
        user_id, application_id, analysis_json=refreshed,
        requirement_state_json={"requirements": refreshed.get("requirements", [])},
        eligibility_state_json={"requirements": [r for r in refreshed.get("requirements", []) if r.get("nature") == "eligibility"]},
        evidence_revision=database.get_user(user_id)["career_revision"],
    )
    target = "NEEDS_CLARIFICATION" if clarification_candidates(refreshed) else "READY_FOR_STRATEGY"
    transition(user_id, application_id, target)
    return database.get_application_workspace(user_id, application_id)


def build_resume_strategy(user_id: str, application_id: str) -> dict:
    workspace = database.get_application_workspace(user_id, application_id)
    if workspace["application_status"] != "READY_FOR_STRATEGY":
        raise RuntimeError("当前状态不能生成策略")
    facts = database.list_facts(user_id, confirmed_only=True, generation_only=True)
    blocks = build_experience_blocks(facts)
    controls = workspace.get("content_controls") or {}
    fact_controls = resolved_fact_controls(blocks, controls)
    strategy_facts = [f for f in facts if fact_controls.get(f["id"], "include") != "skip"]
    base_resume = database.get_resume(user_id, workspace["base_resume_id"])
    strategy = generate_polish_strategy(
        base_resume, workspace["jd_text"], workspace["analysis"], strategy_facts,
        fact_controls, workspace.get("user_preference_prompt", ""), blocks, user_id=user_id,
    )
    strategy["clarification_remaining"] = [r["id"] for r in clarification_candidates(workspace["analysis"])]
    strategy["expected_resume_changes"] = [
        f"{r['relevance']}：{r['recommended_action']} {r['block_id']}" for r in strategy.get("block_ranking", [])
    ]
    database.update_application_fields(user_id, application_id, strategy_json=strategy)
    transition(user_id, application_id, "STRATEGY_READY")
    transition(user_id, application_id, "AWAITING_STRATEGY_APPROVAL")
    return strategy


def approve_strategy(user_id: str, application_id: str) -> dict:
    workspace = database.get_application_workspace(user_id, application_id)
    if workspace["application_status"] != "AWAITING_STRATEGY_APPROVAL" or not workspace.get("strategy"):
        raise RuntimeError("没有可批准的策略")
    database.update_application_fields(user_id, application_id,
                                       strategy_approved_at=datetime.now().isoformat(timespec="seconds"))
    return transition(user_id, application_id, "EDITING")


def reject_strategy(user_id: str, application_id: str, preference: str = "") -> dict:
    if preference:
        database.update_application_fields(user_id, application_id, user_preference_prompt=preference)
    return transition(user_id, application_id, "READY_FOR_STRATEGY")


def generate_target_draft(user_id: str, application_id: str) -> dict:
    workspace = database.get_application_workspace(user_id, application_id)
    if workspace["application_status"] != "EDITING" or not workspace.get("strategy_approved_at"):
        raise RuntimeError("策略未批准，不能生成Draft")
    facts = database.list_facts(user_id, confirmed_only=True, generation_only=True)
    blocks = build_experience_blocks(facts)
    controls = workspace.get("content_controls") or {}
    fact_controls = resolved_fact_controls(blocks, controls)
    usable = [f for f in facts if fact_controls.get(f["id"], "include") != "skip"]
    base = database.get_resume(user_id, workspace["base_resume_id"])
    plan = propose_resume_edits(
        base, workspace["jd_text"], workspace["analysis"], usable, fact_controls,
        workspace.get("user_preference_prompt", ""), workspace["strategy"], user_id=user_id,
    )
    execution = validate_and_apply_edits(
        base["content_text"], plan["edits"], facts, workspace["analysis"]["requirements"],
        fact_controls, base["id"],
    )
    draft = {
        "status": "draft", "content": execution.content, "base_resume_id": base["id"],
        "accepted_edits": execution.applied, "rejected_edits": execution.rejected,
        "control_notes": execution.control_notes, "edit_summary": plan.get("summary", ""),
    }
    database.update_application_fields(user_id, application_id, resume_json=draft)
    transition(user_id, application_id, "DRAFT_READY")
    return draft


def review_draft(user_id: str, application_id: str, review_text: str, decision: str = "reviewed") -> dict:
    workspace = database.get_application_workspace(user_id, application_id)
    if workspace["application_status"] != "DRAFT_READY":
        raise RuntimeError("当前没有可审核Draft")
    database.update_application_fields(user_id, application_id, draft_review_json={
        "content": review_text, "decision": decision, "reviewed_at": datetime.now().isoformat(timespec="seconds")
    })
    return transition(user_id, application_id, "AWAITING_FINAL_APPROVAL")


def approve_final_resume(user_id: str, application_id: str) -> dict:
    workspace = database.get_application_workspace(user_id, application_id)
    if workspace["application_status"] != "AWAITING_FINAL_APPROVAL":
        raise RuntimeError("Draft尚未完成审核")
    content = (workspace.get("draft_review") or {}).get("content") or (workspace.get("resume") or {}).get("content", "")
    if not content.strip():
        raise RuntimeError("最终简历内容为空")
    final = {"content": content, "approved": True}
    database.update_application_fields(
        user_id, application_id, final_resume_json=final,
        final_approved_at=datetime.now().isoformat(timespec="seconds"),
    )
    return transition(user_id, application_id, "FINAL_READY")


def next_action(user_id: str, application_id: str):
    return determine_next_action(database.get_application_workspace(user_id, application_id))
