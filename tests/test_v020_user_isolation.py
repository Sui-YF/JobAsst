from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

import database
import deepseek_client
from identity import resolve_user_id
from llm_provider import DeepSeekProvider, get_provider
from resume_import import save_original_file


def setup_users(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "multi.db")
    database.init_db()
    return database.create_user("User A"), database.create_user("User B")


def confirmed_fact(statement="用户声明事实"):
    return {
        "organization": "IGG", "official_job_title": "Cinematic Designer",
        "fact_type": "实际职责", "evidence_type": "Employment", "statement": statement,
        "skills": [], "verification_status": "confirmed", "evidence_origin": "user_confirmed",
    }


def test_user_a_resume_cannot_be_read_updated_or_deleted_by_user_b(tmp_path, monkeypatch):
    user_a, user_b = setup_users(tmp_path, monkeypatch)
    resume_id = database.add_resume(user_a, "A Resume", "A private content")
    assert database.list_resumes(user_b) == []
    assert database.get_resume(user_b, resume_id) is None
    database.update_resume(user_b, resume_id, "stolen", "changed", "auto")
    assert database.get_resume(user_a, resume_id)["content_text"] == "A private content"
    database.delete_resume(user_b, resume_id)
    assert database.get_resume(user_a, resume_id) is not None


def test_user_a_facts_and_deep_dive_case_never_enter_user_b_pool(tmp_path, monkeypatch):
    user_a, user_b = setup_users(tmp_path, monkeypatch)
    case_id = database.add_experience_case(user_a, {
        "case_type": "Project", "title": "A Case", "summary": "A private case", "project_name": "A Project",
    })
    fact_id = database.add_fact(user_a, confirmed_fact() | {"case_id": case_id})
    assert database.get_experience_case(user_b, case_id) is None
    assert database.list_facts(user_b, confirmed_only=True) == []
    database.update_fact_status(user_b, fact_id, "rejected")
    assert database.list_facts(user_a, confirmed_only=True)[0]["id"] == fact_id
    database.delete_fact(user_b, fact_id)
    assert database.list_facts(user_a, confirmed_only=True)[0]["id"] == fact_id


def test_cross_user_source_and_base_resume_injection_are_rejected(tmp_path, monkeypatch):
    user_a, user_b = setup_users(tmp_path, monkeypatch)
    resume_a = database.add_resume(user_a, "A Resume", "A private content")
    try:
        database.add_or_merge_resume_fact(user_b, confirmed_fact(), resume_a)
        assert False, "B 用户不得引用 A 用户的 Resume 作为 Evidence 来源"
    except PermissionError:
        pass
    try:
        database.save_application(user_b, "AI PM", "Company", "JD", {}, resume_a)
        assert False, "B 用户不得引用 A 用户的 Resume 作为 Base Resume"
    except PermissionError:
        pass


def test_unknown_or_inactive_user_cannot_create_scoped_data(tmp_path, monkeypatch):
    setup_users(tmp_path, monkeypatch)
    try:
        database.add_resume("00000000-0000-4000-8000-000000000001", "X", "X")
        assert False, "不存在的 URL 身份不得创建数据"
    except PermissionError:
        pass


def test_job_application_and_draft_are_scoped_for_read_and_write(tmp_path, monkeypatch):
    user_a, user_b = setup_users(tmp_path, monkeypatch)
    app_id = database.save_application(user_a, "AI PM", "Company", "JD", {"requirements": []})
    assert database.get_application(user_b, app_id) is None
    database.save_resume(user_b, app_id, {"content": "B overwrite"})
    assert database.get_application(user_a, app_id)["resume_json"] is None
    database.save_resume(user_a, app_id, {"content": "A draft"})
    assert "A draft" in database.get_application(user_a, app_id)["resume_json"]


def test_confirm_once_reuses_only_inside_same_user(tmp_path, monkeypatch):
    user_a, user_b = setup_users(tmp_path, monkeypatch)
    a1, created_a1 = database.add_or_merge_user_confirmed_fact(user_a, confirmed_fact())
    a2, created_a2 = database.add_or_merge_user_confirmed_fact(user_a, confirmed_fact())
    b1, created_b1 = database.add_or_merge_user_confirmed_fact(user_b, confirmed_fact())
    assert created_a1 and not created_a2 and a1 == a2
    assert created_b1 and b1 != a1


def test_beta_identity_accepts_only_existing_active_canonical_uuid(tmp_path, monkeypatch):
    user_a, _ = setup_users(tmp_path, monkeypatch)
    assert resolve_user_id(user_a.upper(), "beta") == user_a
    assert resolve_user_id("00000000-0000-4000-8000-000000000001", "beta") is None
    assert resolve_user_id("not-a-uuid", "beta") is None
    assert resolve_user_id(None, "dev") == database.DEV_USER_ID


def test_sqlite_wal_busy_timeout_and_indexes_are_enabled(tmp_path, monkeypatch):
    setup_users(tmp_path, monkeypatch)
    with database._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(career_facts)").fetchall()}
    assert "idx_facts_user_key" in indexes and "idx_facts_user_status" in indexes


def test_upload_path_uses_canonical_system_user_uuid(tmp_path, monkeypatch):
    user_a, _ = setup_users(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    path = save_original_file(user_a.upper(), "RES-1", "resume.docx", b"file")
    saved = __import__("pathlib").Path(path)
    assert saved.parent.name == user_a and saved.name == "RES-1.docx"


def test_provider_abstraction_keeps_deepseek_json_behavior(monkeypatch):
    responses = iter([SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))])])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: next(responses))))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "server-key")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(deepseek_client, "_client", lambda: client)
    provider = get_provider()
    assert isinstance(provider, DeepSeekProvider) and provider.provider_name == "deepseek"
    assert deepseek_client._json_call("prompt", {"x": 1}) == {"ok": True}


def test_core_app_starts_without_importing_optional_ocr_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "no-ocr.db")
    monkeypatch.setenv("APP_MODE", "dev")
    # RapidOCR is lazy-imported only after the optional OCR button is used.
    app_path = __import__("pathlib").Path(__file__).resolve().parents[1] / "app.py"
    page = AppTest.from_file(str(app_path), default_timeout=10).run()
    assert not page.exception
