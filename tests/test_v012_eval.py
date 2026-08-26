from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

import database
import deepseek_client
from resume_editor import validate_and_apply_edits
from scoring import application_advice, calculate_scores


def fact(fid="F1", statement="根据策划与美术反馈拆解需求，完成开发、自测和联调，并根据实际表现持续迭代。"):
    return {
        "id": fid, "organization": "天命战棋", "official_job_title": "独立开发",
        "fact_type": "项目", "statement": statement,
        "skills": ["需求拆解", "开发验证", "反馈迭代"],
        "verification_status": "confirmed", "source_valid": 1, "allowed_for_generation": 1,
        "sources": [],
    }


def test_explicit_ue_and_prompt_are_resume_declared_without_confirmation(monkeypatch):
    resume = "使用 Unreal Engine 完成项目开发\n实际设计并多轮调整 DeepSeek Prompt"
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {"facts": [
        {"organization": "项目", "official_job_title": "开发者", "fact_type": "技能证据",
         "statement": "使用Unreal Engine完成项目开发", "skills": ["Unreal Engine"],
         "source_excerpt": "使用 Unreal Engine 完成项目开发", "evidence_origin": "resume_declared"},
        {"organization": "AI项目", "official_job_title": "开发者", "fact_type": "技能证据",
         "statement": "设计并调整DeepSeek Prompt", "skills": ["Prompt"],
         "source_excerpt": "实际设计并多轮调整 DeepSeek Prompt", "evidence_origin": "resume_declared"},
    ]})
    items = deepseek_client.extract_resume_evidence("Resume", resume)
    assert len(items) == 2
    assert all(item["verification_status"] == "confirmed" for item in items)
    assert all(item["evidence_origin"] == "resume_declared" for item in items)


def test_ai_inference_from_iteration_stays_candidate(monkeypatch):
    resume = "根据策划与美术反馈拆解需求并持续迭代。"
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {"facts": [{
        "organization": "项目", "official_job_title": "开发者", "fact_type": "技能证据",
        "statement": "具备产品需求管理经验", "skills": ["产品需求管理"],
        "source_excerpt": "根据策划与美术反馈拆解需求并持续迭代。", "evidence_origin": "ai_candidate",
    }]})
    item = deepseek_client.extract_resume_evidence("Resume", resume)[0]
    assert item["evidence_origin"] == "ai_candidate"
    assert item["verification_status"] == "needs_confirmation"


def test_expression_gap_has_higher_deep_dive_priority_than_real_gap():
    expression = {"nature": "match", "weight": 3, "match_status": "transferable_match", "gap_type": "expression_gap"}
    real = {"nature": "match", "weight": 3, "match_status": "no_evidence", "gap_type": "real_gap"}
    assert deepseek_client.deep_dive_priority(expression) > deepseek_client.deep_dive_priority(real)
    assert deepseek_client.deep_dive_priority(expression) >= 50


def test_hr_tech_without_adjacent_evidence_is_real_gap(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "requirements": [{
            "text": "3年HR-Tech正式产品经理经验", "criteria": [], "original_text": "3年HR-Tech正式产品经理经验",
            "category": "experience", "priority": "must_have", "nature": "match", "explicit_critical": False,
            "match_status": "no_evidence", "eligibility_status": None, "fact_ids": [], "criteria_coverage": [],
            "gap_type": "real_gap", "reason": "当前资料没有正式HR-Tech经历",
        }], "strengths": [], "weaknesses": [],
    })
    req = deepseek_client.analyze_jd("3年HR-Tech正式产品经理经验", [fact()])["requirements"][0]
    assert req["gap_type"] == "real_gap"
    assert req["match_status"] == "no_evidence"


def test_supported_jd_oriented_reframing_is_allowed():
    original = fact()["statement"]
    proposed = "基于策划与美术反馈开展需求拆解，完成开发验证、自测联调与持续迭代。"
    result = validate_and_apply_edits(original, [{
        "action": "reframe", "target_section": "项目", "target_text": "", "original_text": original,
        "proposed_text": proposed, "fact_ids": ["F1"], "requirement_ids": ["R1"],
        "modification_reason": "突出需求拆解、协作、验证与迭代",
    }], [fact()], [{"id": "R1"}])
    assert len(result.applied) == 1
    assert proposed in result.content


def test_participation_cannot_be_upgraded_to_leading_ai_product():
    limited = fact(statement="参与项目开发。")
    result = validate_and_apply_edits("参与项目开发。", [{
        "action": "rewrite", "target_section": "项目", "target_text": "", "original_text": "参与项目开发。",
        "proposed_text": "主导AI产品从0到1上线。", "fact_ids": ["F1"], "requirement_ids": ["R1"],
        "modification_reason": "贴近JD",
    }], [limited], [{"id": "R1"}])
    assert not result.applied
    assert result.rejected[0]["reason"]


def test_user_preference_is_passed_to_strategy_without_changing_facts(monkeypatch):
    captured = {}
    def fake_call(_prompt, payload):
        captured.update(payload)
        return {"highlight": ["AI项目"], "de_emphasize": [], "never_claim": ["正式AI产品经理经验"],
                "suggest_clarification": [], "summary": "保留UE技术背景"}
    monkeypatch.setattr(deepseek_client, "_json_call", fake_call)
    base = {"id": "R1", "name": "Resume", "content_text": "UE与AI项目"}
    preference = "重点突出AI项目，但保留UE技术背景。"
    deepseek_client.generate_polish_strategy(base, "AI PM JD", {"requirements": []}, [fact()], {}, preference)
    assert captured["user_preference"] == preference
    assert captured["career_evidence"][0]["id"] == "F1"


def test_strategy_string_is_not_split_into_characters(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "highlight": "突出AI求职助手项目", "de_emphasize": "弱化无关细节",
        "never_claim": "不包装正式AI产品经理经验", "suggest_clarification": "补充Prompt迭代案例",
        "summary": "保持真实",
    })
    strategy = deepseek_client.generate_polish_strategy(
        {"id": "R1", "name": "Resume", "content_text": "AI项目"}, "JD", {"requirements": []}, [], {}, ""
    )
    assert strategy["highlight"] == ["突出AI求职助手项目"]
    assert strategy["never_claim"] == ["不包装正式AI产品经理经验"]


def test_reframe_can_preserve_identity_and_date_verbatim():
    base = "IGG\nCinematic Designer\n2022\n根据反馈持续迭代。"
    cited = fact(statement="根据反馈持续迭代。") | {
        "organization": "IGG", "official_job_title": "Cinematic Designer",
    }
    proposed = "IGG\nCinematic Designer\n2022\n根据反馈开展验证并持续迭代。"
    result = validate_and_apply_edits(base, [{
        "action": "reframe", "target_section": "Experience", "target_text": "", "original_text": base,
        "proposed_text": proposed, "fact_ids": ["F1"], "requirement_ids": ["R1"], "modification_reason": "",
    }], [cited], [{"id": "R1"}])
    assert result.applied
    assert "Cinematic Designer" in result.content and "2022" in result.content


def test_application_advice_considers_real_gap_not_score_only():
    requirements = [
        {"nature": "match", "weight": 3, "match_status": "direct_strong", "gap_type": "none"},
        {"nature": "match", "weight": 3, "match_status": "no_evidence", "gap_type": "real_gap"},
    ]
    score = calculate_scores(requirements)
    advice, _ = application_advice(requirements, score, "符合")
    assert advice != "值得申请"


def test_regular_page_hides_engineering_profile_debug(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "ui.db")
    database.init_db()
    database.add_resume(database.DEV_USER_ID, "Resume", "Unreal Engine and Prompt")
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    page = AppTest.from_file(str(app_path), default_timeout=10).run()
    visible = " ".join(str(item.value) for item in [*page.markdown, *page.caption, *page.info])
    assert "职业画像调试信息" not in visible
    assert "source_valid" not in visible


def test_json_call_retries_one_empty_model_response(monkeypatch):
    responses = iter([
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]),
    ])
    completions = SimpleNamespace(create=lambda **_kwargs: next(responses))
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(deepseek_client, "_client", lambda: client)
    assert deepseek_client._json_call("prompt", {}) == {"ok": True}
