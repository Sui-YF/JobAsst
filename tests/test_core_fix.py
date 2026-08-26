import database
import deepseek_client
import sqlite3
from unified_profile import build_experience_blocks, canonical_evidence_pool, resolved_fact_controls


def fact(fid, org, title, statement, fact_type="实际职责", sources=None, education=None):
    return {
        "id": fid, "organization": org, "official_job_title": title,
        "statement": statement, "fact_type": fact_type, "skills": [],
        "verification_status": "confirmed", "sources": sources or [],
        "education": education or {},
    }


def test_cross_resume_igg_is_one_block_and_one_canonical_evidence():
    facts = [
        fact("A", "IGG", "Cinematic Designer", "参与王国纪元买量动画，使用UE完成场景、镜头、角色动画与灯光。", sources=[{"resume_id": "R1"}]),
        fact("B", "IGG", "Cinematic Designer", "参与王国纪元买量动画，使用UE完成场景、镜头、角色动画和灯光。", sources=[{"resume_id": "R2"}]),
    ]
    blocks = build_experience_blocks(facts)
    assert len(blocks) == 1
    assert len(blocks[0]["evidence"]) == 1
    pool = canonical_evidence_pool(facts)
    assert len(pool) == 1
    assert {s["resume_id"] for s in pool[0]["sources"]} == {"R1", "R2"}


def test_numeric_conflict_is_not_semantically_merged():
    facts = [fact("A", "Pratts", "Associate", "带教8名新人"), fact("B", "Pratts", "Associate", "带教20名新人")]
    assert len(build_experience_blocks(facts)[0]["evidence"]) == 2


def test_project_titles_from_two_resumes_form_one_block():
    facts = [
        fact("A", "天命战棋", "独立开发", "完成可运行Demo", "项目"),
        fact("B", "天命战棋", "AI辅助独立开发", "定义MVP并持续迭代", "项目"),
    ]
    blocks = build_experience_blocks(facts)
    assert len(blocks) == 1 and blocks[0]["heading"] == "天命战棋"


def test_evidence_control_overrides_block_control():
    facts = [fact("A", "Pratts", "Associate", "安全监督"), fact("B", "Pratts", "Associate", "新人培训")]
    blocks = build_experience_blocks(facts)
    controls = {f"block:{blocks[0]['id']}": "de_emphasize", "evidence:A": "include"}
    assert resolved_fact_controls(blocks, controls) == {"A": "include", "B": "de_emphasize"}


def test_local_education_keeps_level_and_study_type_separate():
    items = deepseek_client.extract_local_declared_evidence("国家开放大学｜计算机科学与技术 本科（自考在籍）\n2025—2027（预计）")
    edu = next(item for item in items if item["fact_type"] == "教育")
    assert edu["education"]["education_level"] == "本科"
    assert edu["education"]["study_type"] == "自考"
    assert edu["education"]["graduation_status"] == "在籍"


def test_related_experience_is_not_hard_eligibility(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "requirements": [{
            "text": "0-3年产品经理或相关工作经验", "original_text": "0-3年产品经理或相关工作经验",
            "category": "experience", "priority": "must_have", "nature": "eligibility",
            "eligibility_status": "not_met", "match_status": None, "fact_ids": ["A"], "criteria": [],
            "criteria_coverage": [], "explicit_critical": False, "reason": "总工龄超过3年",
        }], "strengths": [], "weaknesses": []
    })
    result = deepseek_client.analyze_jd("0-3年产品经理或相关工作经验", [fact("A", "Demo", "Creator", "产品项目")])
    req = result["requirements"][0]
    assert req["nature"] == "match"
    assert req["eligibility_status"] is None
    assert "不以总工龄" in req["reason"]


def test_bachelor_in_progress_is_evidence_not_no_evidence(monkeypatch):
    monkeypatch.setattr(deepseek_client, "_json_call", lambda *_: {
        "requirements": [{
            "text": "本科及以上学历", "original_text": "本科及以上学历", "category": "education",
            "priority": "must_have", "nature": "eligibility", "eligibility_status": "needs_confirmation",
            "match_status": None, "fact_ids": [], "criteria": [], "criteria_coverage": [],
            "explicit_critical": False, "reason": "career_evidence没有学历",
        }], "strengths": [], "weaknesses": [{"text": "学历硬性条件不满足：未达到本科要求", "fact_ids": []}]
    })
    edu = fact("E", "国家开放大学", "本科", "本科（自考在籍）", "教育", education={
        "education_level": "本科", "study_type": "自考", "graduation_status": "在籍"
    })
    req = deepseek_client.analyze_jd("本科及以上学历", [edu])["requirements"][0]
    assert req["fact_ids"] == ["E"]
    assert req["eligibility_status"] == "needs_confirmation"
    assert "没有学历" not in req["reason"]
    result = deepseek_client.analyze_jd("本科及以上学历", [edu])
    assert "需进一步确认" in result["weaknesses"][0]["title"]


def test_non_destructive_migration_keeps_existing_resume(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE resumes (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'auto',
            original_filename TEXT NOT NULL DEFAULT '', original_file_path TEXT NOT NULL DEFAULT '',
            content_text TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute(
            "INSERT INTO resumes VALUES ('R1','真实简历','auto','','','保留内容','now','now')"
        )
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    assert database.get_resume(database.DEV_USER_ID, "R1")["content_text"] == "保留内容"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM resume_ingestions").fetchone()[0] == 0
