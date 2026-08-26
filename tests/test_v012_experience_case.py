import database
import deepseek_client
from resume_editor import validate_and_apply_edits
from scoring import analysis_delta
from unified_profile import build_experience_blocks

U = database.DEV_USER_ID


def test_one_answer_creates_one_case_and_multiple_evidence(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "case": {"case_type": "Project", "title": "AI求职助手", "summary": "设计并测试JD匹配流程。",
                 "skills": ["Prompt"], "project_name": "AI求职助手", "role": "产品设计"},
        "evidence": [
            {"evidence_type": "Project", "fact_type": "实际职责", "statement": "设计JD匹配流程", "skills": []},
            {"evidence_type": "Skill", "fact_type": "技能证据", "statement": "测试并调整Prompt", "skills": ["Prompt"]},
        ],
    })
    result = deepseek_client.organize_deep_dive_case({"id": "R1"}, "问题", "回答")
    assert result["case"]["title"] == "AI求职助手"
    assert len(result["evidence"]) == 2
    assert all(item["organization"] == "" and item["official_job_title"] == "" for item in result["evidence"])


def test_non_employment_case_uses_evidence_type_not_legacy_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "case.db")
    database.init_db()
    case_id = database.add_experience_case(U, {"case_type": "Project", "title": "Demo", "summary": "项目事实",
                                            "organization": "占位公司", "official_job_title": "占位职位",
                                            "project_name": "Demo"})
    case = database.get_experience_case(U, case_id)
    assert case["organization"] == case["official_job_title"] == ""
    fid = database.add_fact(U, {"evidence_type": "Project", "organization": "", "official_job_title": "",
                             "project_name": "Demo", "fact_type": "项目", "statement": "完成Demo",
                             "verification_status": "confirmed", "evidence_origin": "user_confirmed"})
    block = build_experience_blocks(database.list_facts(U, confirmed_only=True))[0]
    assert block["category"] == "Project" and block["heading"] == "Demo"


def test_employment_case_requires_real_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "employment.db")
    database.init_db()
    try:
        database.add_experience_case(U, {"case_type": "Employment", "title": "工作", "summary": "事实"})
        assert False, "should reject missing identity"
    except ValueError:
        pass


def test_rematch_delta_exposes_requirement_and_score_change():
    before = {"requirements": [{"id": "R1", "text": "Prompt", "nature": "match", "weight": 3, "match_status": "no_evidence"}]}
    after = {"requirements": [{"id": "R1", "text": "Prompt", "nature": "match", "weight": 3, "match_status": "direct_strong"}]}
    delta = analysis_delta(before, after)
    assert delta["score_after"] > delta["score_before"]
    assert delta["changed"][0]["before"] == "no_evidence"


def test_strategy_keeps_valid_block_ranking_only(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "block_ranking": [
            {"block_id": "B1", "relevance": "high", "reason": "直接证明", "positioning": "产品案例", "recommended_action": "expand", "recommended_order": 1},
            {"block_id": "BAD", "relevance": "High", "recommended_action": "remove"},
        ]})
    strategy = deepseek_client.generate_polish_strategy(
        {"id": "R", "name": "Base", "content_text": "x"}, "JD", {"requirements": []}, [], {}, "",
        [{"id": "B1", "heading": "AI项目", "category": "Project", "evidence": []}],
    )
    assert strategy["block_ranking"] == [{"block_id": "B1", "relevance": "High", "reason": "直接证明",
                                           "positioning": "产品案例", "recommended_action": "expand", "recommended_order": 1}]


def test_low_relevance_block_can_be_removed_with_confirmed_evidence():
    fact = {"id": "F1", "organization": "OldCo", "official_job_title": "Worker", "fact_type": "实际职责",
            "statement": "OldCo Worker 2020", "skills": [], "verification_status": "confirmed",
            "source_valid": 1, "allowed_for_generation": 1}
    result = validate_and_apply_edits("OldCo Worker 2020\nRelevant", [{
        "action": "remove", "original_text": "OldCo Worker 2020", "proposed_text": "", "target_text": "",
        "fact_ids": ["F1"], "requirement_ids": ["R1"], "modification_reason": "Low relevance",
    }], [fact], [{"id": "R1"}])
    assert result.applied and "OldCo" not in result.content
