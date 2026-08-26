from types import SimpleNamespace

import database
import eval_harness
from eval_scorecard import HumanScorecard


def test_all_locked_fixtures_and_baselines_pass_offline_without_model(monkeypatch):
    monkeypatch.setattr(eval_harness.deepseek_client, "_json_call",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("offline不得调用模型")))
    report = eval_harness.run_offline()
    assert report["passed"] and report["api_requests"] == 0
    assert len(report["fixtures"]) == 5 and report["assertion_count"] >= 100


def test_fixture_and_prompt_hashes_are_stable():
    fixture = eval_harness.load_fixture("ai_product_manager")
    assert eval_harness.content_hash(fixture) == eval_harness.content_hash(fixture)
    assert len(eval_harness.prompt_hashes()["jd_analysis"]) == 64


def test_targeted_eval_replays_cache_without_second_api_call(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "eval.db")
    monkeypatch.setattr(eval_harness, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(eval_harness, "RESULT_DIR", tmp_path / "results")
    monkeypatch.setattr(eval_harness, "get_provider",
                        lambda: SimpleNamespace(provider_name="mock", model="fixed-model"))
    calls = []
    output = {"requirements": [{"id": "R-01", "text": "AI产品", "nature": "match",
                                 "match_status": "no_evidence", "fact_ids": []}]}
    monkeypatch.setattr(eval_harness.deepseek_client, "analyze_jd",
                        lambda *_args, **_kwargs: calls.append(1) or output)
    first = eval_harness.run_targeted("ai_product_manager")
    second = eval_harness.run_targeted("ai_product_manager")
    assert not first["cache_hit"] and second["cache_hit"]
    assert len(calls) == 1 and second["api_requests"] == 0
    assert first["fixture_hash"] == second["fixture_hash"]
    assert first["prompt_hash"] == second["prompt_hash"]


def test_human_scorecard_is_quality_only_and_has_truthfulness_veto():
    scores = {name: 2 for name in (
        "requirement_coverage", "match_boundary", "evidence_grounding", "truthfulness",
        "positioning_quality", "content_prioritization", "resume_readability",
    )}
    card = HumanScorecard("case", 1, "reviewer", scores, critical_truthfulness_failure=True)
    assert not card.passed
    assert "career" not in card.to_dict() and "evidence" not in card.to_dict()
