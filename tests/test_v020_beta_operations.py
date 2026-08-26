from pathlib import Path
from types import SimpleNamespace

import pytest

import database
import deepseek_client
from identity import resolve_user_id
from llm_provider import DeepSeekProvider, ProviderError, QwenProvider, RateLimitError, get_provider


def setup_users(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "beta.db")
    database.init_db()
    return database.create_user("User A"), database.create_user("User B")


def mock_client(contents, captured=None):
    values = iter(contents)

    def create(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=value))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def configure_qwen(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("QWEN_API_KEY", "qwen-secret")
    monkeypatch.setenv("QWEN_MODEL", "qwen-test-model")
    monkeypatch.setenv("QWEN_BASE_URL", "https://qwen.invalid/compatible/v1")


def test_deepseek_qwen_switch_and_qwen_request_shape(tmp_path, monkeypatch):
    user_a, _ = setup_users(tmp_path, monkeypatch)
    configure_qwen(monkeypatch)
    captured = []
    provider = get_provider()
    assert isinstance(provider, QwenProvider)
    result = provider.json_call("system", {"jd": "text"}, user_id=user_a,
                                operation="analyze_jd", client=mock_client(['{"ok": true}'], captured))
    assert result == {"ok": True}
    assert captured[0]["model"] == "qwen-test-model"
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert captured[0]["messages"][0] == {"role": "system", "content": "system"}
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-test-model")
    assert isinstance(get_provider(), DeepSeekProvider)


def test_provider_switch_does_not_change_business_layer(tmp_path, monkeypatch):
    user_a, _ = setup_users(tmp_path, monkeypatch)
    configure_qwen(monkeypatch)
    response = {
        "requirements": [{"text": "理解 AI 产品", "priority": "must_have", "nature": "match",
                          "match_status": "no_evidence", "fact_ids": [], "criteria": []}],
        "strengths": [], "weaknesses": [],
    }
    monkeypatch.setattr(deepseek_client, "_client", lambda: mock_client([__import__("json").dumps(response)]))
    result = deepseek_client.analyze_jd("JD", [], user_id=user_a)
    assert result["requirements"][0]["text"] == "理解 AI 产品"
    with database._connect() as conn:
        provider = conn.execute("SELECT provider FROM llm_usage WHERE event_type='request'").fetchone()[0]
    assert provider == "qwen"


def test_daily_limit_is_per_user_and_history_reads_do_not_count(tmp_path, monkeypatch):
    user_a, user_b = setup_users(tmp_path, monkeypatch)
    monkeypatch.setenv("DAILY_LLM_REQUEST_LIMIT", "1")
    monkeypatch.setenv("MAX_LLM_RETRIES", "0")
    provider = DeepSeekProvider(api_key="server-key")
    provider.model = "test-model"
    provider.json_call("s", {}, user_id=user_a, operation="one", client=mock_client(['{"ok": 1}']))
    database.list_resumes(user_a)
    database.list_facts(user_a)
    assert database.count_llm_requests(user_a) == 1
    with pytest.raises(RateLimitError, match="今日 AI 调用额度"):
        provider.json_call("s", {}, user_id=user_a, operation="two", client=mock_client(['{"ok": 2}']))
    assert database.count_llm_requests(user_a) == 1
    assert provider.json_call("s", {}, user_id=user_b, operation="one", client=mock_client(['{"ok": 3}']))["ok"] == 3
    assert database.count_llm_requests(user_b) == 1


@pytest.mark.parametrize("contents", [[""], ["not-json"]])
def test_empty_or_invalid_json_fails_safely_and_retry_is_bounded(tmp_path, monkeypatch, contents):
    user_a, _ = setup_users(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_LLM_RETRIES", "1")
    provider = DeepSeekProvider(api_key="server-key")
    provider.model = "test-model"
    calls = []
    with pytest.raises(ProviderError, match="模型暂时没有返回有效结果"):
        provider.json_call("secret prompt", {"private": "payload"}, user_id=user_a,
                           operation="test", client=mock_client(contents * 2, calls))
    assert len(calls) == 2
    assert database.count_llm_requests(user_a) == 2
    with database._connect() as conn:
        event_counts = dict(conn.execute(
            "SELECT event_type, COUNT(*) FROM llm_usage WHERE user_id = ? GROUP BY event_type", (user_a,)
        ).fetchall())
    assert event_counts == {"operation": 1, "request": 2}


def test_revoked_user_is_rejected_by_identity_and_database(tmp_path, monkeypatch):
    user_a, _ = setup_users(tmp_path, monkeypatch)
    resume_id = database.add_resume(user_a, "A", "private")
    assert resolve_user_id(user_a, "beta") == user_a
    assert database.revoke_user(user_a)
    assert resolve_user_id(user_a, "beta") is None
    with pytest.raises(PermissionError):
        database.get_resume(user_a, resume_id)
    with pytest.raises(PermissionError):
        database.list_facts(user_a)


def test_keys_are_not_persisted_or_rendered(tmp_path, monkeypatch):
    setup_users(tmp_path, monkeypatch)
    secret = "sk-never-persist-this"
    provider = DeepSeekProvider(api_key=secret)
    provider.model = "test-model"
    provider.json_call("s", {}, operation="test", client=mock_client(['{"ok": true}']))
    assert secret.encode() not in Path(database.DB_PATH).read_bytes()
    app_text = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY" not in app_text and "QWEN_API_KEY" not in app_text


def test_core_docker_image_excludes_ocr_dependencies():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    requirements = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")
    assert "requirements.txt" in dockerfile and "requirements-ocr.txt" not in dockerfile
    assert "rapidocr" not in requirements and "onnxruntime" not in requirements
    assert ".env" in dockerignore and "data" in dockerignore
