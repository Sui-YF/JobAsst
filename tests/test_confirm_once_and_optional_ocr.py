import builtins
import sys

import pytest

import database
import jd_ocr

U = database.DEV_USER_ID


def candidate(statement="AI归纳出的候选信息"):
    return {
        "organization": "IGG", "official_job_title": "Cinematic Designer",
        "fact_type": "实际职责", "evidence_type": "Employment", "statement": statement,
        "skills": [], "source_excerpt": "Resume原文", "evidence_origin": "ai_candidate",
        "evidence_kind": "candidate", "verification_status": "needs_confirmation",
        "allowed_for_generation": False,
    }


def test_same_candidate_is_not_asked_again_after_resume_reingestion(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reuse.db")
    database.init_db()
    resume_id = database.add_resume(U, "Resume", "Resume原文")
    first_id, first_created = database.add_or_reuse_candidate_fact(U, candidate(), resume_id)
    database.confirm_edited_fact(U, first_id, "IGG", "Cinematic Designer", "AI归纳出的候选信息", [])
    second_id, second_created = database.add_or_reuse_candidate_fact(U, candidate(), resume_id)
    assert first_created and not second_created and first_id == second_id
    stored = next(fact for fact in database.list_facts(U) if fact["id"] == first_id)
    assert stored["verification_status"] == "confirmed"


def test_identical_deep_dive_claim_reuses_confirmed_fact_across_jds(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "deep-dive.db")
    database.init_db()
    payload = candidate("根据欧美市场反馈调整素材") | {
        "evidence_origin": "user_confirmed", "evidence_kind": "direct",
        "verification_status": "confirmed", "allowed_for_generation": True,
    }
    first_id, first_created = database.add_or_merge_user_confirmed_fact(U, payload)
    second_id, second_created = database.add_or_merge_user_confirmed_fact(U, payload | {"source": "另一个JD的Case"})
    assert first_created and not second_created and first_id == second_id
    assert len(database.list_facts(U, confirmed_only=True)) == 1


def test_rejected_identical_candidate_is_not_recreated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "rejected.db")
    database.init_db()
    resume_id = database.add_resume(U, "Resume", "Resume原文")
    fact_id, _ = database.add_or_reuse_candidate_fact(U, candidate(), resume_id)
    database.update_fact_status(U, fact_id, "rejected")
    reused_id, created = database.add_or_reuse_candidate_fact(U, candidate(), resume_id)
    assert reused_id == fact_id and not created
    stored = next(fact for fact in database.list_facts(U) if fact["id"] == fact_id)
    assert stored["verification_status"] == "rejected"


def test_same_resume_excerpt_does_not_reask_when_ai_rephrases(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "rephrase.db")
    database.init_db()
    resume_id = database.add_resume(U, "Resume", "Resume原文")
    fact_id, _ = database.add_or_reuse_candidate_fact(U, candidate("第一种AI归纳"), resume_id)
    database.confirm_edited_fact(U, fact_id, "IGG", "Cinematic Designer", "用户确认后的表达", [])
    reused_id, created = database.add_or_reuse_candidate_fact(U, candidate("第二种AI归纳"), resume_id)
    assert reused_id == fact_id and not created


def test_ocr_missing_is_optional_and_returns_actionable_message(monkeypatch):
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name in {"rapidocr", "numpy", "PIL"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(jd_ocr.OCRError, match="仍可直接粘贴"):
        jd_ocr.extract_jd_text([("jd.png", b"not-used")])
