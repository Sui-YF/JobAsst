from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path
from typing import Any

import database
import deepseek_client
from eval_assertions import evaluate_fixture
from llm_provider import get_provider


ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "evals" / "fixtures"
BASELINE_DIR = ROOT / "evals" / "baselines"
CACHE_DIR = ROOT / "evals" / "cache"
RESULT_DIR = ROOT / "evals" / "results"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def prompt_hashes() -> dict[str, str]:
    prompts = {
        "jd_analysis": deepseek_client.ANALYSIS_PROMPT,
        "deep_dive_question": deepseek_client.DEEP_DIVE_QUESTION_PROMPT,
        "deep_dive_case": deepseek_client.ANSWER_EXTRACTION_PROMPT,
        "block_ranking": deepseek_client.STRATEGY_PROMPT,
        "resume_edit": deepseek_client.EDIT_PROMPT,
    }
    return {name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in prompts.items()}


def load_fixture(fixture_id: str) -> dict:
    matches = sorted(FIXTURE_DIR.glob(f"{fixture_id}.v*.json"))
    if not matches:
        raise FileNotFoundError(f"找不到Fixture：{fixture_id}")
    fixture = json.loads(matches[-1].read_text(encoding="utf-8"))
    if fixture.get("locked") is not True:
        raise ValueError("Baseline Fixture必须locked=true；真实数据变化应创建新版本")
    return fixture


def load_baseline(fixture: dict) -> dict:
    path = BASELINE_DIR / f"{fixture['fixture_id']}.v{fixture['fixture_version']}.json"
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if baseline.get("fixture_version") != fixture.get("fixture_version"):
        raise ValueError("Baseline与Fixture版本不一致")
    return baseline


def run_offline(fixture_ids: list[str] | None = None) -> dict:
    selected = fixture_ids or sorted(path.name.split(".v", 1)[0] for path in FIXTURE_DIR.glob("*.json"))
    report = {"mode": "offline", "api_requests": 0, "prompt_hashes": prompt_hashes(), "fixtures": []}
    for fixture_id in selected:
        fixture = load_fixture(fixture_id)
        baseline = load_baseline(fixture)
        checks = evaluate_fixture(fixture, baseline)
        report["fixtures"].append({
            "fixture_id": fixture_id, "fixture_version": fixture["fixture_version"],
            "fixture_hash": content_hash(fixture), "baseline_hash": content_hash(baseline),
            "passed": all(check.passed for check in checks),
            "checks": [check.to_dict() for check in checks],
        })
    report["passed"] = all(item["passed"] for item in report["fixtures"])
    report["assertion_count"] = sum(len(item["checks"]) for item in report["fixtures"])
    return report


def _analysis_diff(baseline: dict, current: dict) -> str:
    before = json.dumps(baseline, ensure_ascii=False, sort_keys=True, indent=2).splitlines()
    after = json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2).splitlines()
    return "\n".join(difflib.unified_diff(before, after, fromfile="baseline", tofile="current", lineterm=""))


def run_targeted(fixture_id: str) -> dict:
    database.init_db()
    fixture = load_fixture(fixture_id)
    baseline = load_baseline(fixture)
    provider = get_provider()
    model = getattr(provider, "model", "unknown")
    hashes = prompt_hashes()
    key_payload = {
        "fixture_hash": content_hash(fixture), "prompt_hash": hashes["jd_analysis"],
        "provider": provider.provider_name, "model": model, "stage": "jd_analysis",
    }
    cache_key = content_hash(key_payload)
    cache_path = CACHE_DIR / f"{cache_key}.json"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    before_requests = database.count_llm_requests(database.DEV_USER_ID)
    cache_hit = cache_path.exists()
    if cache_hit:
        current = json.loads(cache_path.read_text(encoding="utf-8"))["output"]
    else:
        current = deepseek_client.analyze_jd(
            fixture["input"]["jd_text"], fixture["input"]["career_evidence"],
            user_id=database.DEV_USER_ID,
        )
        cache_path.write_text(json.dumps({
            "cache_key": cache_key, "metadata": key_payload, "output": current,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    after_requests = database.count_llm_requests(database.DEV_USER_ID)
    result = {
        "fixture_id": fixture_id, "fixture_version": fixture["fixture_version"],
        **key_payload, "cache_key": cache_key, "cache_hit": cache_hit,
        "api_requests": after_requests - before_requests,
        "diff": _analysis_diff(baseline["outputs"]["analysis"], current),
        "output": current,
    }
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / f"{fixture_id}.latest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Prompt/Match/Resume Polish Eval Harness")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--targeted", nargs="*", default=[])
    args = parser.parse_args()
    if args.offline:
        report = run_offline()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] and report["api_requests"] == 0 else 1
    if args.targeted:
        for fixture_id in args.targeted:
            result = run_targeted(fixture_id)
            print(json.dumps({key: result[key] for key in (
                "fixture_id", "fixture_hash", "prompt_hash", "provider", "model",
                "cache_hit", "api_requests", "cache_key",
            )}, ensure_ascii=False, indent=2))
        return 0
    parser.error("请选择--offline或--targeted")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
