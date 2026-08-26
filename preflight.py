from __future__ import annotations

import re
from collections import Counter

import database
from application_workflow import transition


PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def _issue(code: str, level: str, message: str) -> dict:
    return {"code": code, "level": level, "message": message}


def evaluate_preflight(workspace: dict, facts: list[dict]) -> dict:
    issues = []
    final = workspace.get("final_resume") or {}
    content = str(final.get("content", "")).strip()
    fact_map = {f["id"]: f for f in facts}
    draft = workspace.get("resume") or {}
    accepted = draft.get("accepted_edits", draft.get("applied_edits", []))
    rejected = draft.get("rejected_edits", [])
    controls = workspace.get("content_controls") or {}
    requirements = (workspace.get("analysis") or {}).get("requirements", [])

    if not content:
        issues.append(_issue("NO_FINAL_CONTENT", "error", "尚无经过用户确认的最终简历。"))
    for edit in accepted:
        refs = edit.get("fact_ids", [])
        invalid = [fid for fid in refs if fid not in fact_map]
        if invalid:
            issues.append(_issue("INVALID_FACT_REFERENCE", "error", "存在无效的职业信息引用。"))
        if any(fact_map.get(fid, {}).get("verification_status") != "confirmed" for fid in refs):
            issues.append(_issue("UNCONFIRMED_EVIDENCE", "error", "最终内容使用了尚未确认的信息。"))
        if any(controls.get(fid) == "skip" for fid in refs):
            issues.append(_issue("SKIP_VIOLATION", "error", "最终内容使用了你要求跳过的信息。"))
    for fid, choice in controls.items():
        statement = fact_map.get(fid, {}).get("statement", "")
        if choice == "must_include" and statement and statement not in content and not any(fid in e.get("fact_ids", []) for e in accepted):
            issues.append(_issue("MUST_INCLUDE_MISSING", "error", "一项必须保留的经历未进入最终简历。"))
    for fact in facts:
        title = fact.get("official_job_title", "").strip()
        organization = fact.get("organization", "").strip()
        if title and organization and organization in content and title not in content:
            issues.append(_issue("JOB_TITLE_PROTECTION", "warning", f"{organization} 的正式职位未在对应内容中出现，请人工确认。"))

    critical = [r for r in requirements if r.get("nature") == "match" and int(r.get("weight", 1)) >= 3]
    uncovered = [r for r in critical if r.get("match_status") not in {"direct_strong", "direct_partial", "transferable_match"}]
    if uncovered:
        issues.append(_issue("CRITICAL_REQUIREMENT_GAP", "warning", f"仍有 {len(uncovered)} 项关键岗位要求缺少证据。"))
    eligibility = [r for r in requirements if r.get("nature") == "eligibility" and r.get("eligibility_status") != "met"]
    if eligibility:
        issues.append(_issue("ELIGIBILITY_WARNING", "warning", f"存在 {len(eligibility)} 项硬性资格风险或待确认项。"))
    real_gaps = sum(r.get("gap_type") == "real_gap" for r in requirements)
    expression_gaps = sum(r.get("gap_type") == "expression_gap" for r in requirements)
    if real_gaps:
        issues.append(_issue("REAL_GAP", "warning", f"仍有 {real_gaps} 项真实能力缺口。"))
    if expression_gaps:
        issues.append(_issue("EXPRESSION_GAP", "warning", f"仍有 {expression_gaps} 项表达证据缺口。"))
    if content and not (PHONE.search(content) or EMAIL.search(content)):
        issues.append(_issue("CONTACT_MISSING", "warning", "未检测到电话或邮箱，请确认联系方式。"))
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    normalized = [re.sub(r"\W+", "", line.lower()) for line in lines]
    duplicates = [line for line, count in Counter(normalized).items() if line and count > 1]
    if duplicates:
        issues.append(_issue("DUPLICATE_BULLETS", "warning", "检测到重复内容。"))
    if len(content) > 9000 or len(lines) > 100:
        issues.append(_issue("RESUME_TOO_LONG", "warning", "简历可能过长，建议压缩。"))
    if any(len(line) > 300 for line in lines):
        issues.append(_issue("FORMATTING", "warning", "存在异常长段落，导出前请检查格式。"))
    if rejected:
        issues.append(_issue("GUARDRAIL_REJECTIONS", "warning", f"真实性保护拦截了 {len(rejected)} 项修改。"))

    status = "NOT_READY" if any(i["level"] == "error" for i in issues) else (
        "READY_WITH_WARNINGS" if issues else "READY"
    )
    return {"status": status, "issues": issues, "checked_without_llm": True}


def run_preflight(user_id: str, application_id: str) -> dict:
    workspace = database.get_application_workspace(user_id, application_id)
    if workspace["application_status"] not in {"FINAL_READY", "PREFLIGHT_READY"}:
        raise RuntimeError("只有已确认的Final Resume可以运行投递前检查")
    result = evaluate_preflight(workspace, database.list_facts(user_id, confirmed_only=True))
    database.update_application_fields(user_id, application_id, preflight_json=result)
    if workspace["application_status"] == "FINAL_READY":
        transition(user_id, application_id, "PREFLIGHT_READY")
    return result
