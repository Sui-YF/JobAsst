import database
import deepseek_client

U = database.DEV_USER_ID
from resume_editor import validate_and_apply_edits


def resume_fact(statement="向中国员工解释仓库安全要求"):
    return {
        "organization": "Pratts",
        "official_job_title": "Warehouse Associate",
        "fact_type": "实际职责",
        "statement": statement,
        "skills": ["Safety Communication"],
        "source_excerpt": statement,
    }


def test_resume_update_invalidates_old_declared_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "truth.db")
    database.init_db()
    resume_id = database.add_resume(U, "Resume", "旧内容：仓库安全沟通")
    fact_id, _ = database.add_or_merge_resume_fact(U, resume_fact(), resume_id)
    assert any(f["id"] == fact_id for f in database.list_facts(U, confirmed_only=True, generation_only=True))

    database.update_resume(U, resume_id, "Resume", "完全替换后的新内容", "auto")

    assert all(f["id"] != fact_id for f in database.list_facts(U, confirmed_only=True))
    stored = next(f for f in database.list_facts(U) if f["id"] == fact_id)
    assert stored["source_valid"] == 0
    assert stored["allowed_for_generation"] == 0
    assert database.resume_needs_ingestion(U, resume_id, "new-hash", deepseek_client.PARSER_VERSION)

    reactivated_id, created = database.add_or_merge_resume_fact(U, resume_fact(), resume_id)
    assert reactivated_id == fact_id and not created
    assert any(f["id"] == fact_id for f in database.list_facts(U, confirmed_only=True, generation_only=True))


def test_deleted_resume_orphan_cannot_generate(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "truth.db")
    database.init_db()
    resume_id = database.add_resume(U, "Resume", "仓库安全沟通")
    fact_id, _ = database.add_or_merge_resume_fact(U, resume_fact(), resume_id)

    database.delete_resume(U, resume_id)

    assert all(f["id"] != fact_id for f in database.list_facts(U, confirmed_only=True, generation_only=True))


def test_deleting_one_of_two_resume_sources_keeps_fact(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "truth.db")
    database.init_db()
    first = database.add_resume(U, "A", "仓库安全沟通")
    second = database.add_resume(U, "B", "仓库安全沟通")
    fact_id, _ = database.add_or_merge_resume_fact(U, resume_fact(), first)
    database.add_or_merge_resume_fact(U, resume_fact(), second)

    database.delete_resume(U, first)

    kept = next(f for f in database.list_facts(U, confirmed_only=True, generation_only=True) if f["id"] == fact_id)
    assert {source["resume_id"] for source in kept["sources"]} == {second}


def test_user_confirmed_fact_survives_without_resume_source(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "truth.db")
    database.init_db()
    fact_id = database.add_fact(U, {
        **resume_fact("用户确认自己带教过新员工"),
        "verification_status": "confirmed",
        "evidence_origin": "user_confirmed",
        "source": "用户经历深挖确认",
    })
    assert any(f["id"] == fact_id for f in database.list_facts(U, confirmed_only=True, generation_only=True))


def test_resume_declared_fact_without_valid_source_cannot_generate(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "truth.db")
    database.init_db()
    fact_id = database.add_fact(U, {
        **resume_fact(),
        "verification_status": "confirmed",
        "evidence_origin": "resume_declared",
        "source": "不存在的Resume来源",
    })
    stored = next(f for f in database.list_facts(U) if f["id"] == fact_id)
    assert stored["source_valid"] == 0
    assert stored["allowed_for_generation"] == 0
    assert all(f["id"] != fact_id for f in database.list_facts(U, confirmed_only=True, generation_only=True))


def test_valid_fact_id_cannot_authorize_semantic_expansion():
    fact = {
        "id": "CF-1", "organization": "Pratts", "official_job_title": "Warehouse Associate",
        "statement": "向中国员工解释仓库安全要求", "skills": ["Safety Communication"],
        "verification_status": "confirmed",
    }
    result = validate_and_apply_edits(
        "向中国员工解释仓库安全要求",
        [{
            "action": "rewrite", "target_section": "Experience", "target_text": "",
            "original_text": "向中国员工解释仓库安全要求",
            "proposed_text": "主导AI产品战略并负责路线图决策",
            "fact_ids": ["CF-1"], "requirement_ids": ["R-1"], "modification_reason": "",
        }],
        [fact], [{"id": "R-1"}],
    )
    assert not result.applied
    assert "扩展" in result.rejected[0]["reason"] or "语义" in result.rejected[0]["reason"]


def test_unknown_study_type_is_needs_confirmation(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "requirements": [{
            "text": "全日制本科及以上学历", "original_text": "必须具有全日制本科及以上学历",
            "category": "education", "priority": "must_have", "nature": "eligibility",
            "eligibility_status": "not_met", "match_status": None, "fact_ids": [], "criteria": [],
            "criteria_coverage": [], "explicit_critical": False, "reason": "学习形式不明",
        }], "strengths": [], "weaknesses": [],
    })
    education = {
        "id": "EDU-1", "organization": "Example University", "official_job_title": "本科",
        "fact_type": "教育", "statement": "计算机科学本科", "skills": [],
        "verification_status": "confirmed",
        "education": {"education_level": "本科", "study_type": "unknown", "graduation_status": "毕业"},
    }
    req = deepseek_client.analyze_jd("必须具有全日制本科及以上学历", [education])["requirements"][0]
    assert req["eligibility_status"] == "needs_confirmation"
    assert "不能直接判定" in req["reason"]


def test_zero_to_three_year_related_experience_is_not_hard_eligibility(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "requirements": [{
            "text": "0–3年相关经验", "original_text": "0–3年相关经验",
            "category": "experience", "priority": "must_have", "nature": "eligibility",
            "eligibility_status": "not_met", "match_status": None, "fact_ids": [], "criteria": [],
            "criteria_coverage": [], "explicit_critical": False, "reason": "总工龄超过3年",
        }], "strengths": [], "weaknesses": [],
    })
    req = deepseek_client.analyze_jd("0–3年相关经验", [])["requirements"][0]
    assert req["nature"] == "match"
    assert req["eligibility_status"] is None
