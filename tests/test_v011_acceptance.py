from io import BytesIO
from types import SimpleNamespace

from PIL import Image

import database
import deepseek_client
from jd_ocr import extract_jd_text
from resume_editor import validate_and_apply_edits

U = database.DEV_USER_ID


def confirmed_fact(fact_id="CF-1", statement="负责跨团队沟通、培训和执行协调。"):
    return {
        "id": fact_id,
        "organization": "Demo Co",
        "official_job_title": "Coordinator",
        "fact_type": "实际职责",
        "statement": statement,
        "skills": ["Communication", "Coordination", "Training"],
        "verification_status": "confirmed",
        "evidence_origin": "resume_declared",
        "evidence_kind": "direct",
        "sources": [],
    }


def test_1_resume_bachelor_is_user_declared_evidence(monkeypatch):
    resume_text = "教育经历：Example University，本科学历"
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "facts": [{
            "organization": "Example University", "official_job_title": "本科",
            "fact_type": "教育", "statement": "拥有本科学历", "skills": [],
            "source_excerpt": "本科学历", "evidence_origin": "resume_declared", "restrictions": "",
        }]
    })
    facts = deepseek_client.extract_resume_evidence("AI PM Resume", resume_text)
    assert facts[0]["evidence_origin"] == "resume_declared"
    assert facts[0]["verification_status"] == "confirmed"


def test_2_deep_dive_preserves_8_person_limit_and_no_review_authority(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "facts": [
            {"organization": "Pratts", "official_job_title": "Associate", "fact_type": "实际职责", "statement": "负责约8人小组的新人带教和安全监督", "skills": ["Training", "Safety"], "evidence_kind": "transferable", "allowed_for_generation": True, "restrictions": "不得扩大为20人团队"},
            {"organization": "Pratts", "official_job_title": "Associate", "fact_type": "限制或负面事实", "statement": "没有绩效考核权限", "skills": [], "evidence_kind": "negative", "allowed_for_generation": False, "restrictions": "禁止写成具有人事考核权限"},
        ]
    })
    candidates = deepseek_client.extract_deep_dive_candidates(
        {"text": "管理20人以上团队"}, "请描述实际团队规模和权限", "负责8人小组，没有绩效考核权限"
    )
    assert any("8人" in fact["statement"] for fact in candidates)
    assert any("没有绩效" in fact["statement"] and not fact["allowed_for_generation"] for fact in candidates)
    assert all("20人" not in fact["statement"] for fact in candidates)


def test_3_transferable_skill_is_not_direct_ai_product_experience(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "requirements": [{
            "text": "跨部门推动AI产品落地", "criteria": ["跨部门协作", "AI产品落地"],
            "original_text": "跨部门推动AI产品落地", "category": "experience", "priority": "must_have",
            "nature": "match", "explicit_critical": False, "match_status": "transferable_match",
            "eligibility_status": None, "fact_ids": ["CF-1"], "criteria_coverage": [],
            "reason": "可证明沟通协调和执行能力，但不是AI产品直接经验",
        }], "strengths": [], "weaknesses": []
    })
    analysis = deepseek_client.analyze_jd("跨部门推动AI产品落地", [confirmed_fact()])
    assert analysis["requirements"][0]["match_status"] == "transferable_match"
    assert "不是AI产品直接经验" in analysis["requirements"][0]["reason"]


def test_4_related_ai_criteria_are_grouped_into_one_requirement(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "requirements": [{
            "text": "AI技术理解与落地能力",
            "criteria": ["LLM", "多模态", "内容识别", "AI能力边界", "AI落地路径"],
            "original_text": "LLM、多模态、内容识别、能力边界与落地路径", "category": "skill",
            "priority": "must_have", "nature": "match", "explicit_critical": False,
            "match_status": "no_evidence", "eligibility_status": None, "fact_ids": [],
            "criteria_coverage": [], "reason": "暂无证据",
        }], "strengths": [], "weaknesses": []
    })
    analysis = deepseek_client.analyze_jd("LLM 多模态 内容识别 AI能力边界 AI落地路径", [])
    assert len(analysis["requirements"]) == 1
    assert len(analysis["requirements"][0]["criteria"]) == 5


def test_5_fact_from_resume_b_is_available_when_base_is_resume_a(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    resume_a = database.add_resume(U, "Resume A", "General resume")
    resume_b = database.add_resume(U, "Resume B", "Unique AI project")
    fact_id, _ = database.add_or_merge_resume_fact(U, {
        "organization": "Personal Project", "official_job_title": "Creator", "fact_type": "项目",
        "statement": "从0到1开发AI求职助手", "skills": ["AI Product"], "source_excerpt": "Unique AI project",
    }, resume_b)
    evidence = database.list_facts(U, confirmed_only=True)
    assert resume_a != resume_b
    assert any(f["id"] == fact_id and any(s["resume_id"] == resume_b for s in f["sources"]) for f in evidence)


def test_6_skip_content_does_not_appear_in_draft():
    fact = confirmed_fact(statement="不相关兼职经历")
    fact["sources"] = [{"resume_id": "RES-A", "source_excerpt": "不相关兼职经历"}]
    result = validate_and_apply_edits("核心经历\n不相关兼职经历", [], [fact], [], {"CF-1": "skip"}, "RES-A")
    assert "不相关兼职经历" not in result.content


def test_7_must_include_is_enforced_from_confirmed_evidence():
    fact = confirmed_fact(statement="AI求职助手项目")
    result = validate_and_apply_edits("Base Resume", [], [fact], [], {"CF-1": "must_include"}, "RES-A")
    assert "AI求职助手项目" in result.content


def test_8_multiple_screenshots_are_merged_for_user_review(monkeypatch):
    calls = iter([SimpleNamespace(txts=["岗位名称：AI PM", "职责一"]), SimpleNamespace(txts=["任职要求", "本科以上"] )])
    fake_module = SimpleNamespace(RapidOCR=lambda: lambda _image: next(calls))
    monkeypatch.setitem(__import__("sys").modules, "rapidocr", fake_module)
    image = Image.new("RGB", (20, 20), "white")
    buffer = BytesIO(); image.save(buffer, format="PNG")
    text = extract_jd_text([("1.png", buffer.getvalue()), ("2.png", buffer.getvalue())])
    assert "截图1" in text and "截图2" in text
    assert "岗位名称：AI PM" in text and "本科以上" in text


def test_9_user_edit_is_the_only_confirmed_candidate_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    fact_id = database.add_fact(U, {
        "organization": "Pratts", "official_job_title": "Associate", "fact_type": "实际职责",
        "statement": "管理8人并负责排班", "skills": [], "verification_status": "needs_confirmation",
        "evidence_origin": "ai_candidate", "source": "deep dive",
    })
    database.confirm_edited_fact(U, fact_id, "Pratts", "Associate", "带教8人，但没有排班权", ["Training"])
    saved = next(f for f in database.list_facts(U, confirmed_only=True) if f["id"] == fact_id)
    assert saved["statement"] == "带教8人，但没有排班权"
    assert "负责排班" not in saved["statement"]


def test_10_unsupported_new_claim_is_blocked():
    result = validate_and_apply_edits(
        "Base Resume", [{
            "action": "add_from_confirmed_fact", "target_section": "", "target_text": "",
            "original_text": "", "proposed_text": "管理20人团队", "fact_ids": ["CF-MISSING"],
            "requirement_ids": ["R-1"], "modification_reason": "",
        }], [confirmed_fact()], [{"id": "R-1"}],
    )
    assert not result.applied
    assert result.rejected


def test_same_fact_from_two_resumes_merges_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    r1 = database.add_resume(U, "A", "带教8名新人")
    r2 = database.add_resume(U, "B", "带教8名新人")
    payload = {"organization": "Pratts", "official_job_title": "Associate", "fact_type": "实际职责",
               "statement": "带教8名新人", "skills": ["Training"], "source_excerpt": "带教8名新人"}
    fact_id, created = database.add_or_merge_resume_fact(U, payload, r1)
    merged_id, created_again = database.add_or_merge_resume_fact(U, payload, r2)
    fact = next(f for f in database.list_facts(U) if f["id"] == fact_id)
    assert created and not created_again and merged_id == fact_id
    assert {s["resume_id"] for s in fact["sources"]} == {r1, r2}


def test_conflicting_numeric_resume_claim_waits_for_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    r1 = database.add_resume(U, "A", "带教8名新人")
    r2 = database.add_resume(U, "B", "带教20名新人")
    common = {"organization": "Pratts", "official_job_title": "Associate", "fact_type": "实际职责", "skills": ["Training"]}
    database.add_or_merge_resume_fact(U, {**common, "statement": "带教8名新人", "source_excerpt": "带教8名新人"}, r1)
    conflict_id, _ = database.add_or_merge_resume_fact(U, {**common, "statement": "带教20名新人", "source_excerpt": "带教20名新人"}, r2)
    conflict = next(f for f in database.list_facts(U) if f["id"] == conflict_id)
    assert conflict["verification_status"] == "needs_confirmation"
    assert conflict["allowed_for_generation"] == 0
    assert "潜在冲突" in conflict["restrictions"]
    database.confirm_edited_fact(U, conflict_id, "Pratts", "Associate", "带教20名新人（用户已核实）", ["Training"])
    confirmed = next(f for f in database.list_facts(U, confirmed_only=True, generation_only=True) if f["id"] == conflict_id)
    assert confirmed["evidence_origin"] == "user_confirmed"
