"""Real V0.1.2 smoke test using local Resumes and an existing AI-PM JD."""

import json

import database
from deepseek_client import (
    analyze_jd,
    deep_dive_priority,
    generate_deep_dive_question,
    generate_polish_strategy,
    propose_resume_edits,
    sync_all_resume_evidence,
)
from resume_editor import validate_and_apply_edits
from scoring import application_advice, calculate_eligibility, calculate_scores
from unified_profile import build_experience_blocks, canonical_evidence_pool, resolved_fact_controls


database.init_db()
user_id = database.DEV_USER_ID
resumes = database.list_resumes(user_id)
assert len(resumes) >= 2, "Need the current two real Resumes"

with database._connect() as conn:
    jobs = [dict(row) for row in conn.execute(
        "SELECT * FROM job_applications WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)
    ).fetchall()]
    job = next((row for row in jobs if "ai" in row["job_title"].lower() and "产品经理" in row["job_title"]), None)
assert job, "No existing AI product manager JD found"

sync_result = sync_all_resume_evidence(user_id, resumes, database)
raw_facts = database.list_facts(user_id, confirmed_only=True)
pool = canonical_evidence_pool(raw_facts)
analysis = analyze_jd(job["jd_text"], pool)
scores = calculate_scores(analysis["requirements"])
eligibility = calculate_eligibility(analysis["requirements"])
advice, advice_reason = application_advice(analysis["requirements"], scores, eligibility)

deep_dive_options = sorted(
    [req for req in analysis["requirements"] if deep_dive_priority(req) >= 50],
    key=deep_dive_priority,
    reverse=True,
)
deep_dive = generate_deep_dive_question(deep_dive_options[0], resumes, raw_facts) if deep_dive_options else {}

base_resume = database.get_resume(user_id, job.get("base_resume_id")) if job.get("base_resume_id") else None
if not base_resume:
    base_resume = next((resume for resume in resumes if "ai" in resume["name"].lower()), resumes[0])

used_ids = {fid for req in analysis["requirements"] for fid in req.get("fact_ids", [])}
blocks = build_experience_blocks(database.list_facts(user_id, confirmed_only=True, generation_only=True))
block_controls = {}
for block in blocks:
    ids = {fid for item in block["evidence"] for fid in item["fact_ids"]}
    block_controls[f"block:{block['id']}"] = "include" if ids & used_ids else "de_emphasize"
fact_controls = resolved_fact_controls(blocks, block_controls)
generation_facts = database.list_facts(user_id, confirmed_only=True, generation_only=True)
planner_facts = [fact for fact in generation_facts if fact_controls.get(fact["id"]) != "skip"]
preference = "重点突出AI项目，但保留UE技术背景。"
strategy = generate_polish_strategy(
    base_resume, job["jd_text"], analysis, planner_facts, fact_controls, preference, blocks
)
plan = propose_resume_edits(
    base_resume, job["jd_text"], analysis, planner_facts, fact_controls, preference, strategy
)
result = validate_and_apply_edits(
    base_resume["content_text"], plan["edits"], generation_facts,
    analysis["requirements"], fact_controls, base_resume["id"],
)

gap_counts = {}
status_counts = {}
for req in analysis["requirements"]:
    gap_counts[req.get("gap_type", "none")] = gap_counts.get(req.get("gap_type", "none"), 0) + 1
    status = req.get("match_status") or req.get("eligibility_status")
    status_counts[status] = status_counts.get(status, 0) + 1

print(json.dumps({
    "resume_count": len(resumes),
    "sync": sync_result,
    "evidence_pool_count": len(pool),
    "requirement_count": len(analysis["requirements"]),
    "score": scores.match_score,
    "coverage": scores.evidence_coverage,
    "eligibility": eligibility,
    "advice": advice,
    "advice_reason": advice_reason,
    "status_counts": status_counts,
    "gap_counts": gap_counts,
    "deep_dive_question": deep_dive.get("question", ""),
    "strategy": strategy,
    "block_ranking": [
        {"heading": next((b["heading"] for b in blocks if b["id"] == item["block_id"]), item["block_id"]), **item}
        for item in strategy.get("block_ranking", [])
    ],
    "preference": preference,
    "proposed_edits": len(plan["edits"]),
    "applied_edits": len(result.applied),
    "rejected_edits": len(result.rejected),
    "rejected_reasons": [item["reason"] for item in result.rejected],
    "draft_changed": result.content != base_resume["content_text"],
}, ensure_ascii=False, indent=2))
