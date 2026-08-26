from __future__ import annotations

from io import BytesIO
from pathlib import Path

from docx import Document
from streamlit.testing.v1 import AppTest

import database

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
U = database.DEV_USER_ID


def _app(tmp_path, monkeypatch) -> AppTest:
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state-test.db")
    database.init_db()
    return AppTest.from_file(str(APP_PATH), default_timeout=10).run()


def _button(at: AppTest, label: str):
    return next(button for button in at.button if button.label == label)


def test_a_pasted_resume_saves_and_form_values_survive_rerun(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch)
    at.text_input(key="add_resume_name").set_value("UE Resume")
    at.text_area(key="add_resume_text").set_value("GAS and C++ project experience")
    _button(at, "添加到我的简历").click()
    at.run()

    assert at.text_input(key="add_resume_name").value == "UE Resume"
    assert at.text_area(key="add_resume_text").value == "GAS and C++ project experience"
    assert any(resume["name"] == "UE Resume" for resume in database.list_resumes(U))
    assert any("简历已添加" in success.value for success in at.success)


def test_a2_add_second_resume_when_manage_selectbox_already_has_state(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state-test.db")
    database.init_db()
    first_id = database.add_resume(U, "Existing Resume", "Existing content")
    at = AppTest.from_file(str(APP_PATH), default_timeout=10).run()
    assert at.selectbox(key="manage_resume_id").value == first_id

    at.text_input(key="add_resume_name").set_value("Second Resume")
    at.text_area(key="add_resume_text").set_value("Second content")
    _button(at, "添加到我的简历").click()
    at.run()

    assert len(database.list_resumes(U)) == 2
    assert at.selectbox(key="manage_resume_id").value != first_id
    assert at.text_input(key="add_resume_name").value == "Second Resume"


def test_b_validation_error_keeps_name(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch)
    at.text_input(key="add_resume_name").set_value("Name must survive")
    _button(at, "添加到我的简历").click()
    at.run()

    assert at.text_input(key="add_resume_name").value == "Name must survive"
    assert any("请填写简历名称" in error.value for error in at.error)
    assert database.list_resumes(U) == []


def test_c_docx_upload_persists_original_and_extracted_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    at = _app(tmp_path, monkeypatch)
    document = Document()
    document.add_paragraph("Uploaded DOCX Resume")
    buffer = BytesIO()
    document.save(buffer)

    at.text_input(key="add_resume_name").set_value("DOCX Resume")
    at.file_uploader(key="add_resume_upload").set_value(
        ("resume.docx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    )
    _button(at, "添加到我的简历").click()
    at.run()

    saved = database.list_resumes(U)
    assert len(saved) == 1
    assert "Uploaded DOCX Resume" in saved[0]["content_text"]
    assert saved[0]["original_filename"] == "resume.docx"
    assert saved[0]["original_file_path"]
    assert (tmp_path / saved[0]["original_file_path"]).is_file()
    assert at.file_uploader(key="add_resume_upload").value is not None


def test_d_database_error_keeps_user_input(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch)
    at.text_input(key="add_resume_name").set_value("Retry Resume")
    at.text_area(key="add_resume_text").set_value("Do not lose this content")
    monkeypatch.setattr(database, "add_resume", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db down")))
    _button(at, "添加到我的简历").click()
    at.run()

    assert at.text_input(key="add_resume_name").value == "Retry Resume"
    assert at.text_area(key="add_resume_text").value == "Do not lose this content"
    assert any("db down" in error.value for error in at.error)


def test_e_sqlite_data_survives_new_app_session(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch)
    at.text_input(key="add_resume_name").set_value("Persistent Resume")
    at.text_area(key="add_resume_text").set_value("Stored in SQLite")
    _button(at, "添加到我的简历").click()
    at.run()

    restarted = AppTest.from_file(str(APP_PATH), default_timeout=10).run()
    assert restarted.selectbox(key="manage_resume_id").value == database.list_resumes(U)[0]["id"]
    assert any(resume["content_text"] == "Stored in SQLite" for resume in database.list_resumes(U))
