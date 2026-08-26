import copy

import deepseek_client
from resume_editor import validate_and_apply_edits


def claim(fid="F1", statement="用户声明的经历", **extra):
    return {
        "id": fid, "organization": extra.pop("organization", "IGG"),
        "official_job_title": extra.pop("official_job_title", "Cinematic Designer"),
        "fact_type": "实际职责", "evidence_type": "Employment", "statement": statement,
        "skills": [], "verification_status": "confirmed", "source_valid": 1,
        "allowed_for_generation": 1, "evidence_kind": "direct", "restrictions": "", **extra,
    }


def requirement(status, fact_ids=None, signatures=None):
    return {"id": "R-01", "text": "AI产品能力", "nature": "match", "weight": 3,
            "match_status": status, "fact_ids": fact_ids or [], "evidence_signatures": signatures or {},
            "gap_type": "none" if status == "direct_strong" else "expression_gap", "reason": ""}


def test_incremental_rematch_does_not_randomly_downgrade_unchanged_evidence():
    old = claim()
    new = claim("F2", "补充了一个相关案例")
    signature = deepseek_client._evidence_signature(old)
    before = {"requirements": [requirement("direct_strong", ["F1"], {"F1": signature})]}
    after = {"requirements": [requirement("transferable_match", ["F2"])]}
    result = deepseek_client.stabilize_incremental_analysis(before, after, [old, new])
    assert result["requirements"][0]["match_status"] == "direct_strong"
    assert set(result["requirements"][0]["fact_ids"]) == {"F1", "F2"}


def test_real_evidence_change_allows_downgrade():
    old = claim()
    signature = deepseek_client._evidence_signature(old)
    changed = copy.deepcopy(old) | {"statement": "用户修改后的不同声明"}
    before = {"requirements": [requirement("direct_strong", ["F1"], {"F1": signature})]}
    after = {"requirements": [requirement("transferable_match", ["F1"])]}
    result = deepseek_client.stabilize_incremental_analysis(before, after, [changed])
    assert result["requirements"][0]["match_status"] == "transferable_match"


def test_new_confirmed_conflict_allows_downgrade():
    old = claim()
    conflict = claim("F2", "用户明确声明没有相关权限", evidence_kind="negative", allowed_for_generation=0)
    signature = deepseek_client._evidence_signature(old)
    before = {"requirements": [requirement("direct_strong", ["F1"], {"F1": signature})]}
    after = {"requirements": [requirement("not_met", ["F2"])]}
    result = deepseek_client.stabilize_incremental_analysis(before, after, [old, conflict])
    assert result["requirements"][0]["match_status"] == "not_met"


def test_resume_and_deep_dive_claims_have_equal_matching_authority(monkeypatch):
    origins = []
    monkeypatch.setattr(deepseek_client, "_json_call", lambda _prompt, payload: origins.extend(
        item.get("evidence_origin", "") for item in payload["career_evidence"]
    ) or {"requirements": [{"text": "素材优化", "original_text": "素材优化", "category": "skill",
                             "priority": "must_have", "nature": "match", "match_status": "direct_strong",
                             "fact_ids": ["R", "D"]}]})
    facts = [claim("R", evidence_origin="resume_declared"), claim("D", evidence_origin="user_confirmed")]
    result = deepseek_client.analyze_jd("素材优化", facts)
    assert result["requirements"][0]["fact_ids"] == ["R", "D"]


def test_igg_data_feedback_positioning_allowed_without_fact_upgrade():
    fact = claim(statement="在IGG查看点击率、CTR和下载率，并根据欧美市场反馈调整素材。",
                 context="根据欧美市场的点击率和下载率反馈调整素材。")
    original = "在IGG根据反馈调整素材。"
    proposed = "具备跨国游戏公司工作经历，基于投放数据反馈持续优化素材。"
    result = validate_and_apply_edits(original, [{"action": "reframe", "original_text": original,
        "target_text": "", "proposed_text": proposed, "fact_ids": ["F1"], "requirement_ids": ["R1"]}],
        [fact], [{"id": "R1"}])
    assert result.applied


def test_positioning_still_rejects_unclaimed_growth_and_team_size():
    fact = claim(statement="在IGG根据欧美市场反馈调整素材。")
    result = validate_and_apply_edits("根据反馈调整素材。", [{"action": "rewrite",
        "original_text": "根据反馈调整素材。", "target_text": "",
        "proposed_text": "主导10人团队实现CTR提升30%。", "fact_ids": ["F1"], "requirement_ids": ["R1"]}],
        [fact], [{"id": "R1"}])
    assert not result.applied


def test_project_resume_claim_needs_no_company_or_job_title(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {"facts": [{
        "evidence_type": "Project", "organization": "", "official_job_title": "",
        "project_name": "AI求职助手", "role": "产品设计", "fact_type": "项目",
        "statement": "设计AI求职助手", "skills": [], "source_excerpt": "设计AI求职助手",
        "evidence_origin": "resume_declared"}]})
    result = deepseek_client.extract_resume_evidence("Resume", "设计AI求职助手")
    assert len(result) == 1 and result[0]["verification_status"] == "confirmed"
    assert result[0]["organization"] == result[0]["official_job_title"] == ""
