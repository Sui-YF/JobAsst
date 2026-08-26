from __future__ import annotations

import argparse
import json
from pathlib import Path

import career_agent
import database
from eval_harness import load_fixture
from preflight import run_preflight
from resume_export import export_application_docx
from resume_templates import recommend_template, select_template


def run_case(fixture_id: str) -> dict:
    fixture = load_fixture(fixture_id)
    cached = json.loads((Path("evals/results") / f"{fixture_id}.latest.json").read_text(encoding="utf-8"))
    user_id = database.create_user(f"E2E {fixture_id}")
    base = fixture["input"]["base_resume"]
    resume_id = database.add_resume(user_id, base["name"], base["content_text"])
    for item in fixture["input"]["career_evidence"]:
        payload = dict(item)
        # Fixture origins test equal authority; in the production DB each imported snapshot is
        # explicitly confirmed by this isolated E2E user so it has a valid reusable source.
        payload.update(fact_type=payload.get("fact_type", "其他"), source="E2E User Confirmed",
                       evidence_origin="user_confirmed", verification_status="confirmed")
        database.add_fact(user_id, payload)
    app_id = database.save_application(
        user_id, fixture["label"], "E2E Company", fixture["input"]["jd_text"], cached["output"], resume_id
    )
    strategy = career_agent.build_resume_strategy(user_id, app_id)
    career_agent.approve_strategy(user_id, app_id)
    draft = career_agent.generate_target_draft(user_id, app_id)
    career_agent.review_draft(user_id, app_id, draft["content"])
    career_agent.approve_final_resume(user_id, app_id)
    preflight = run_preflight(user_id, app_id)
    template = recommend_template(fixture["label"])
    select_template(user_id, app_id, template["template_id"])
    export_path, export_bytes = export_application_docx(user_id, app_id, "E2E")
    reloaded = database.get_application_workspace(user_id, app_id)
    return {
        "fixture_id": fixture_id, "user_id": user_id, "application_id": app_id,
        "strategy": bool(strategy), "accepted_edits": len(draft["accepted_edits"]),
        "rejected_edits": len(draft["rejected_edits"]), "preflight": preflight["status"],
        "template": template["template_id"], "docx_bytes": len(export_bytes),
        "export_path": export_path, "reloaded_state": reloaded["application_status"],
        "final_persisted": bool(reloaded.get("final_resume", {}).get("content")),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixtures", nargs="*", default=["ue_ai_assisted_development", "ai_product_manager"])
    parser.add_argument("--db", default="data/e2e_v03.db")
    args = parser.parse_args()
    database.DB_PATH = Path(args.db)
    database.init_db()
    results = [run_case(fixture_id) for fixture_id in args.fixtures]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item["final_persisted"] and item["reloaded_state"] == "PREFLIGHT_READY" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
