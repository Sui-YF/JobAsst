"""Manual DeepSeek smoke test with synthetic facts; does not touch app.db."""

from deepseek_client import (
    analyze_jd,
    extract_deep_dive_candidates,
    generate_deep_dive_question,
    propose_resume_edits,
)
from resume_editor import validate_and_apply_edits
from scoring import calculate_eligibility, calculate_scores


facts = [
    {
        "id": "CF-DEMO-001",
        "organization": "Demo Warehouse",
        "official_job_title": "Warehouse Associate",
        "fact_type": "实际职责",
        "statement": "向中文员工解释仓库安全要求，并带新员工熟悉安全流程。",
        "skills": ["Safety Communication", "Onboarding"],
        "verification_status": "confirmed",
        "restrictions": "不得改写为 Manager 或 Supervisor",
    }
]
jd = "招聘双语仓库安全协调员。核心职责是向员工传达安全要求并培训新员工。普通话优先。必须持有有效 OSHA 证书。"

analysis = analyze_jd(jd, facts)
scores = calculate_scores(analysis["requirements"])
eligibility = calculate_eligibility(analysis["requirements"])
base_resume = {
    "id": "RES-DEMO",
    "name": "Demo Resume",
    "content_text": "Demo Warehouse\nWarehouse Associate\n向中文员工解释仓库安全要求，并带新员工熟悉安全流程。",
}
edit_plan = propose_resume_edits(base_resume, jd, analysis, facts)
resume = validate_and_apply_edits(base_resume["content_text"], edit_plan["edits"], facts, analysis["requirements"])

missing_requirement = {
    "id": "REQ-DEEP-DIVE",
    "text": "具备正式团队绩效考核权限",
    "nature": "match",
    "match_status": "no_evidence",
}
deep_dive = generate_deep_dive_question(missing_requirement, [base_resume], facts)
candidates = extract_deep_dive_candidates(
    missing_requirement,
    deep_dive["question"],
    "我在Demo Warehouse带过8名新员工熟悉安全流程，但没有排班权，也没有绩效考核权限。",
    [base_resume],
    facts,
)

assert analysis["requirements"]
assert all(r["weight"] in {1, 3, 4} for r in analysis["requirements"])
assert all(
    set(r["fact_ids"]).issubset({"CF-DEMO-001"})
    for r in analysis["requirements"]
)
assert all(set(edit.get("fact_ids", [])).issubset({"CF-DEMO-001"}) for edit in resume.applied)
assert candidates
assert all(candidate["verification_status"] == "needs_confirmation" for candidate in candidates)
assert any("没有" in candidate["statement"] or candidate["evidence_kind"] == "negative" for candidate in candidates)

print("AI requirements:", len(analysis["requirements"]))
print("Rule score:", scores.match_score)
print("Evidence coverage:", scores.evidence_coverage)
print("Eligibility:", eligibility)
print("Applied edits:", len(resume.applied))
print("Rejected edits:", len(resume.rejected))
print("Deep-dive candidates:", len(candidates))
print("Demo AI smoke test: PASS")
