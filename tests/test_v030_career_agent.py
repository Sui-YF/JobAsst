import json
from pathlib import Path

import pytest
from docx import Document

import career_agent
import database
from application_workflow import InvalidTransition, determine_next_action, transition
from jd_fetch import JDFetchError, _validate_public_url
from preflight import evaluate_preflight, run_preflight
from resume_export import export_application_docx
from resume_import import extract_resume_text
from resume_templates import select_template


def setup_workspace(tmp_path, monkeypatch, analysis=None):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "v03.db")
    monkeypatch.chdir(tmp_path)
    database.init_db()
    user = database.create_user("Beta")
    resume = database.add_resume(user, "Base", "Name\nemail@example.com\nIGG\nCinematic Designer\n参与素材制作。")
    app = database.save_application(user, "AI产品经理", "Demo", "AI产品JD", analysis or {
        "requirements": [{"id": "R-01", "text": "AI产品", "nature": "match", "weight": 3,
                          "match_status": "direct_partial", "fact_ids": [], "gap_type": "expression_gap"}]
    }, resume)
    return user, resume, app


def fact(statement="参与素材制作。", fact_id=None):
    return {"id": fact_id, "organization": "IGG", "official_job_title": "Cinematic Designer",
            "fact_type": "实际职责", "evidence_type": "Employment", "statement": statement,
            "skills": [], "verification_status": "confirmed", "evidence_origin": "user_confirmed"}


def test_state_machine_persists_and_rejects_illegal_transition(tmp_path, monkeypatch):
    user, _, app = setup_workspace(tmp_path, monkeypatch)
    assert database.get_application_workspace(user, app)["application_status"] == "READY_FOR_STRATEGY"
    transition(user, app, "STRATEGY_READY")
    assert database.get_application_workspace(user, app)["application_status"] == "STRATEGY_READY"
    with pytest.raises(InvalidTransition):
        transition(user, app, "FINAL_READY")
    assert determine_next_action(database.get_application_workspace(user, app)).action == "review_strategy"


def test_expression_gap_routes_to_clarification_and_skip_routes_strategy(tmp_path, monkeypatch):
    user, resume, _ = setup_workspace(tmp_path, monkeypatch)
    app = database.create_application_workspace(user, "AI PM", "Demo", "JD", resume)
    monkeypatch.setattr(career_agent, "analyze_jd", lambda *_args, **_kwargs: {"requirements": [
        {"id": "R-01", "text": "AI", "nature": "match", "weight": 3, "match_status": "direct_partial",
         "fact_ids": ["F"], "gap_type": "expression_gap"}
    ]})
    workspace = career_agent.analyze_application(user, app)
    assert workspace["application_status"] == "NEEDS_CLARIFICATION"
    assert career_agent.skip_clarification(user, app)["application_status"] == "READY_FOR_STRATEGY"


def test_unconfirmed_deep_dive_is_not_truth_and_confirm_triggers_rematch(tmp_path, monkeypatch):
    user, _, app = setup_workspace(tmp_path, monkeypatch)
    database.update_application_fields(user, app, application_status="NEEDS_CLARIFICATION")
    pending = fact("可能的AI经历") | {"verification_status": "needs_confirmation", "evidence_origin": "ai_candidate"}
    database.add_fact(user, pending)
    assert all(item["statement"] != "可能的AI经历" for item in database.list_facts(user, confirmed_only=True))
    monkeypatch.setattr(career_agent, "analyze_jd", lambda *_args, **_kwargs: {"requirements": [
        {"id": "R-01", "text": "AI", "nature": "match", "weight": 3, "match_status": "direct_strong",
         "fact_ids": [], "gap_type": "none"}
    ]})
    case = {"case_type": "Project", "title": "AI案例", "summary": "确认的AI项目", "skills": []}
    evidence = [{"evidence_type": "Project", "fact_type": "项目", "statement": "确认的AI项目", "skills": []}]
    result = career_agent.confirm_experience_case(user, app, case, evidence)
    assert any(item["statement"] == "确认的AI项目" for item in database.list_facts(user, confirmed_only=True))
    assert result["application_status"] == "READY_FOR_STRATEGY"


def test_strategy_and_final_approval_gates(tmp_path, monkeypatch):
    user, _, app = setup_workspace(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError):
        career_agent.generate_target_draft(user, app)
    database.update_application_fields(user, app, strategy_json={"headline": "AI"}, application_status="AWAITING_STRATEGY_APPROVAL")
    career_agent.approve_strategy(user, app)
    assert database.get_application_workspace(user, app)["application_status"] == "EDITING"
    database.update_application_fields(user, app, resume_json={"content": "Draft"}, application_status="DRAFT_READY")
    with pytest.raises(RuntimeError):
        career_agent.approve_final_resume(user, app)
    career_agent.review_draft(user, app, "Final content")
    assert career_agent.approve_final_resume(user, app)["application_status"] == "FINAL_READY"


def test_career_truth_change_marks_old_application_for_rematch(tmp_path, monkeypatch):
    user, _, app = setup_workspace(tmp_path, monkeypatch)
    database.add_fact(user, fact("新的真实经历"))
    assert database.get_application_workspace(user, app)["application_status"] == "NEEDS_REMATCH"


def test_preflight_detects_eligibility_gap_and_rejected_claim(tmp_path, monkeypatch):
    analysis = {"requirements": [
        {"id": "R-01", "nature": "eligibility", "eligibility_status": "needs_confirmation", "weight": 3},
        {"id": "R-02", "nature": "match", "match_status": "no_evidence", "weight": 3, "gap_type": "real_gap"},
    ]}
    user, _, app = setup_workspace(tmp_path, monkeypatch, analysis)
    database.update_application_fields(user, app, resume_json={"content": "Draft", "rejected_edits": [{"reason": "unsupported"}]},
                                       final_resume_json={"content": "Name\nemail@example.com"}, application_status="FINAL_READY")
    result = run_preflight(user, app)
    codes = {item["code"] for item in result["issues"]}
    assert {"ELIGIBILITY_WARNING", "REAL_GAP", "GUARDRAIL_REJECTIONS"} <= codes
    assert result["status"] == "READY_WITH_WARNINGS" and result["checked_without_llm"]


def test_template_change_preserves_truth_match_and_docx_export(tmp_path, monkeypatch):
    user, _, app = setup_workspace(tmp_path, monkeypatch)
    before_facts = database.list_facts(user)
    before_analysis = database.get_application(user, app)["analysis_json"]
    database.update_application_fields(user, app, final_resume_json={"content": "Name\nemail@example.com\nAI Product"},
                                       application_status="FINAL_READY")
    select_template(user, app, "ai_product")
    path, content = export_application_docx(user, app, "测试用户")
    assert Path(path).exists() and content.startswith(b"PK")
    assert database.list_facts(user) == before_facts
    assert database.get_application(user, app)["analysis_json"] == before_analysis
    assert "CF-" not in Document(path).paragraphs[0].text


def test_revoked_and_cross_user_application_access_remain_blocked(tmp_path, monkeypatch):
    user, _, app = setup_workspace(tmp_path, monkeypatch)
    other = database.create_user("Other")
    assert database.get_application_workspace(other, app) is None
    database.revoke_user(user)
    with pytest.raises(PermissionError):
        database.get_application_workspace(user, app)


def test_upload_limit_and_ssrf_guards(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    with pytest.raises(ValueError):
        extract_resume_text("resume.exe", b"x")
    with pytest.raises(ValueError):
        extract_resume_text("resume.txt", b"x" * (1024 * 1024 + 1))
    with pytest.raises(JDFetchError):
        _validate_public_url("file:///etc/passwd")
    monkeypatch.setattr("jd_fetch.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("127.0.0.1", 80))])
    with pytest.raises(JDFetchError):
        _validate_public_url("http://example.com/job")
