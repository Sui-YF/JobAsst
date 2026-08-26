from __future__ import annotations

import json
import hashlib
import os
import re
from contextvars import ContextVar
from typing import Any

import database
from llm_provider import ProviderError, get_provider
from scoring import requirement_weight


class DeepSeekError(RuntimeError):
    pass


_call_user_id: ContextVar[str] = ContextVar("llm_user_id", default=database.DEV_USER_ID)
_call_operation: ContextVar[str] = ContextVar("llm_operation", default="unknown")


def is_configured() -> bool:
    return get_provider().is_configured()


def _client():
    return get_provider().create_client()


def _json_call(
    system_prompt: str, user_payload: dict[str, Any],
) -> dict:
    try:
        return get_provider().json_call(
            system_prompt, user_payload, user_id=_call_user_id.get(),
            operation=_call_operation.get(), client=_client()
        )
    except DeepSeekError:
        raise
    except ProviderError as exc:
        raise DeepSeekError(str(exc)) from exc
    except Exception as exc:
        raise DeepSeekError("模型暂时没有返回有效结果，请稍后重试。") from exc


def _business_json_call(
    system_prompt: str, user_payload: dict[str, Any], user_id: str, operation: str
) -> dict:
    user_token = _call_user_id.set(user_id)
    operation_token = _call_operation.set(operation)
    try:
        return _json_call(system_prompt, user_payload)
    finally:
        _call_user_id.reset(user_token)
        _call_operation.reset(operation_token)


ANALYSIS_PROMPT = """
你是个人求职助手中的结构化分析模块。界面语言为中文，JD可以是中文或英文。
只能使用输入中的career_evidence。Evidence可能来自Base Resume、其他Resume或用户确认补充。
Resume明确内容属于User-Declared Evidence，可以直接参与匹配。不得创造事实，不计算百分比。

按照“招聘方实际评估的独立能力维度”聚合JD，不要按关键词机械拆分。
一个Requirement可以包含多个criteria。例如LLM、多模态、内容识别、能力边界、落地路径应优先聚合为“AI技术理解与落地能力”。
只有确实属于两个独立招聘判断标准时才拆成两个Requirement。

每项返回：
- id: R-01格式
- text: 中文标准化的独立能力维度
- criteria: 该Requirement内部具体判断点的字符串数组；Criteria不单独扩大评分权重
- original_text: JD原文依据
- category: core_responsibility/skill/experience/education/language/certification/location_work_authorization/other_hard_constraint
- priority: must_have/preferred/core_responsibility/supporting
- nature: match或eligibility
- explicit_critical: JD是否明确强调为关键；程序不会自动采用权重4
- match_status: direct_strong/direct_partial/transferable_match/no_evidence/not_met/needs_confirmation；eligibility填null
- eligibility_status: met/not_met/needs_confirmation；match填null
- fact_ids: 只能引用输入中存在且可用的Fact ID
- criteria_coverage: [{"criterion":"","status":"covered/partial/not_covered","fact_ids":[]}]
- gap_type: none/expression_gap/real_gap/qualification_gap
- reason: 中文解释，明确Direct或Transferable边界

Direct Strong必须有直接事实覆盖核心criteria；Direct Partial有直接事实但范围、深度或criteria不完整。
Transferable Match表示不同职位或行业中的事实能证明可迁移能力，但不能宣称直接拥有目标行业经验。
简历没写不等于Not Met；Not Met只有明确负面证据才可使用，否则使用No Evidence。
Expression Gap表示已有相邻或部分Evidence，可能通过经历深挖和真实重述改善。
Real Gap表示全部Evidence都没有直接或合理相邻场景，不能靠润色解决。
Eligibility风险统一使用qualification_gap。Direct Strong使用none。
正式职位与实际职责分开，不得把带教或监督职责改写成正式Manager/Supervisor/Team Lead。
普通工作年限、行业经验、产品经验默认是match Requirement，不是Hard Eligibility；只有JD明确写成不可替代/不满足不可申请时才可设为eligibility。
“0-3年产品经理或相关工作经验”判断的是相关经验，不是总工龄；总工龄超过3年不能据此判Not Met。
教育层级和学习形式分开判断。“本科及以上”优先判断education_level；只有JD明确要求全日制/统招时才比较study_type。在籍/预计毕业必须如实显示，不能写成无学历证据。

同时返回strengths和weaknesses，优势必须有Fact ID支撑。
严格输出JSON对象：{"requirements":[],"strengths":[],"weaknesses":[]}。
"""


def analyze_jd(
    jd_text: str, facts: list[dict], user_id: str = database.DEV_USER_ID,
    operation: str = "JD_ANALYSIS",
) -> dict:
    matching_evidence = [
        {
            key: fact.get(key)
            for key in (
                "id", "organization", "official_job_title", "fact_type", "statement", "skills",
                "verification_status", "evidence_origin", "evidence_kind", "restrictions", "education", "experience_block",
                "evidence_type", "project_name", "role", "date_range", "context",
            )
            if fact.get(key) not in (None, "", [], {})
        }
        for fact in facts
    ]
    raw = _business_json_call(
        ANALYSIS_PROMPT, {"jd": jd_text, "career_evidence": matching_evidence},
        user_id, operation,
    )
    valid_fact_ids = {f["id"] for f in facts if f.get("verification_status") == "confirmed"}
    requirements = []
    seen_texts = set()
    for index, item in enumerate(raw.get("requirements", []), start=1):
        text = str(item.get("text", "")).strip()
        if not text or text.lower() in seen_texts:
            continue
        seen_texts.add(text.lower())
        nature = item.get("nature") if item.get("nature") in {"match", "eligibility"} else "match"
        priority = item.get("priority")
        if priority not in {"must_have", "preferred", "core_responsibility", "supporting"}:
            priority = "supporting"
        # Weight 4 is only a suggestion until the user explicitly confirms it.
        critical_suggested = bool(item.get("explicit_critical", False))
        explicit_critical = False
        fact_ids = [fid for fid in item.get("fact_ids", []) if fid in valid_fact_ids]
        match_status = item.get("match_status")
        if nature == "match":
            if match_status not in {"direct_strong", "direct_partial", "transferable_match", "no_evidence", "not_met", "needs_confirmation"}:
                match_status = "no_evidence"
            if match_status in {"direct_strong", "direct_partial", "transferable_match", "not_met"} and not fact_ids:
                match_status = "no_evidence"
        else:
            match_status = None
        eligibility_status = item.get("eligibility_status")
        if nature == "eligibility" and eligibility_status not in {"met", "not_met", "needs_confirmation"}:
            eligibility_status = "needs_confirmation"
        requirements.append(
            {
                "id": f"R-{index:02d}",
                "text": text,
                "criteria": [str(x).strip() for x in item.get("criteria", []) if str(x).strip()],
                "original_text": str(item.get("original_text", "")),
                "category": str(item.get("category", "skill")),
                "priority": priority,
                "nature": nature,
                "explicit_critical": explicit_critical,
                "critical_weight_suggested": critical_suggested,
                "weight": requirement_weight(priority, False),
                "match_status": match_status,
                "eligibility_status": eligibility_status if nature == "eligibility" else None,
                "fact_ids": fact_ids,
                "criteria_coverage": item.get("criteria_coverage", []),
                "gap_type": _normalize_gap_type(item.get("gap_type"), nature, match_status, bool(fact_ids)),
                "reason": str(item.get("reason", "")),
            }
        )
    _apply_requirement_guardrails(requirements, facts)
    fact_map = {fact["id"]: fact for fact in facts}
    for req in requirements:
        req["evidence_signatures"] = {
            fid: _evidence_signature(fact_map[fid]) for fid in req.get("fact_ids", []) if fid in fact_map
        }
    if not requirements:
        raise DeepSeekError("未能从 JD 中提取有效要求，请检查 JD 后重试。")
    strengths = [_normalize_insight(x) for x in raw.get("strengths", [])]
    weaknesses = [_normalize_insight(x) for x in raw.get("weaknesses", [])]
    strengths = [item for item in strengths if item][:5]
    weaknesses = [item for item in weaknesses if item][:5]
    if any(
        req.get("category") == "education" and req.get("eligibility_status") == "needs_confirmation"
        for req in requirements
    ):
        for item in weaknesses:
            title = item.get("title", "")
            if "学历" in title and any(term in title for term in ("不满足", "未达到", "无学历")):
                item["title"] = "本科为在籍/预计毕业状态；是否满足岗位对毕业状态的要求需进一步确认。"
    return {
        "requirements": requirements,
        "strengths": strengths,
        "weaknesses": weaknesses,
    }


def _evidence_signature(fact: dict) -> str:
    fields = ("statement", "organization", "official_job_title", "evidence_type", "project_name", "role",
              "date_range", "evidence_kind", "restrictions", "verification_status", "source_valid")
    payload = {key: fact.get(key) for key in fields}
    payload["skills"] = sorted(str(value) for value in fact.get("skills", []))
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def stabilize_incremental_analysis(before: dict, after: dict, current_facts: list[dict]) -> dict:
    """Prevent model-only downgrades while allowing real Evidence changes or conflicts."""
    rank = {"no_evidence": 0, "needs_confirmation": 0, "not_met": 0,
            "transferable_match": 1, "direct_partial": 2, "direct_strong": 3}
    fact_map = {fact["id"]: fact for fact in current_facts}
    old_by_id = {req.get("id"): req for req in before.get("requirements", [])}
    for new_req in after.get("requirements", []):
        old_req = old_by_id.get(new_req.get("id"))
        if not old_req or old_req.get("nature") != "match" or new_req.get("nature") != "match":
            continue
        old_status, new_status = old_req.get("match_status"), new_req.get("match_status")
        if rank.get(new_status, 0) >= rank.get(old_status, 0):
            continue
        signatures = old_req.get("evidence_signatures") or {}
        old_ids = old_req.get("fact_ids", [])
        old_evidence_unchanged = bool(old_ids) and bool(signatures) and all(
            fid in fact_map and signatures.get(fid) == _evidence_signature(fact_map[fid]) for fid in old_ids
        )
        cited_new = [fact_map[fid] for fid in new_req.get("fact_ids", []) if fid in fact_map and fid not in old_ids]
        has_new_conflict = any(
            fact.get("evidence_kind") == "negative" or "冲突" in str(fact.get("restrictions", ""))
            for fact in cited_new
        )
        if old_evidence_unchanged and not has_new_conflict:
            new_req["match_status"] = old_status
            new_req["fact_ids"] = list(dict.fromkeys(old_ids + new_req.get("fact_ids", [])))
            new_req["evidence_signatures"] = {
                fid: _evidence_signature(fact_map[fid]) for fid in new_req["fact_ids"] if fid in fact_map
            }
            new_req["gap_type"] = old_req.get("gap_type", new_req.get("gap_type"))
            new_req["reason"] = "原有Evidence仍有效；保留原匹配等级，并合并新增Evidence。" + new_req.get("reason", "")
    return after


def _apply_requirement_guardrails(requirements: list[dict], facts: list[dict]) -> None:
    """Deterministic corrections for known high-impact model interpretation errors."""
    education_facts = [f for f in facts if f.get("fact_type") == "教育" and f.get("verification_status") == "confirmed"]
    for req in requirements:
        wording = f"{req.get('text', '')} {req.get('original_text', '')}"
        experience_wording = any(term in wording.lower() for term in ("工作经验", "产品经验", "行业经验", "相关经验", "experience"))
        explicit_hard_gate = any(term in wording.lower() for term in (
            "必须", "硬性", "至少", "不少于", "不得低于", "minimum", "must have", "required",
        ))
        if req.get("nature") == "eligibility" and experience_wording and not explicit_hard_gate:
            req["nature"] = "match"
            req["eligibility_status"] = None
            req["match_status"] = "direct_partial" if req.get("fact_ids") else "no_evidence"
            req["reason"] = "该项判断相关经验，不以总工龄作硬性淘汰。" + req.get("reason", "")
            req["gap_type"] = "expression_gap" if req.get("fact_ids") else "real_gap"
        if req.get("category") != "education" and not any(term in wording for term in ("学历", "本科", "大专", "硕士")):
            continue
        levels = {f.get("education", {}).get("education_level") for f in education_facts}
        bachelor_facts = [f for f in education_facts if f.get("education", {}).get("education_level") in {"本科", "硕士", "博士"}]
        if "本科" in wording and bachelor_facts:
            req["fact_ids"] = list(dict.fromkeys(req.get("fact_ids", []) + [f["id"] for f in bachelor_facts]))
            study_types = {f.get("education", {}).get("study_type", "unknown") for f in bachelor_facts}
            statuses = {f.get("education", {}).get("graduation_status", "unknown") for f in bachelor_facts}
            if any(term in wording for term in ("全日制", "统招")) and not (study_types & {"全日制", "统招"}):
                if "unknown" in study_types:
                    status = "needs_confirmation"
                    reason = "存在本科层级Evidence，但学习形式未知；不能直接判定不满足全日制/统招要求。"
                else:
                    status = "not_met"
                    reason = "存在本科层级Evidence，且已确认的学习形式不满足JD明确要求的全日制/统招条件。"
            elif statuses <= {"在籍", "unknown"}:
                status = "needs_confirmation"
                reason = "Resume明确声明本科在籍；学历层级有Evidence，是否满足该岗位对毕业状态的要求需确认。"
            else:
                status = "met"
                reason = "Resume-Declared Education显示本科及以上层级。"
            req["nature"] = "eligibility"
            req["match_status"] = None
            req["eligibility_status"] = status
            req["gap_type"] = "qualification_gap"
            req["reason"] = reason


def _normalize_gap_type(value: object, nature: str, match_status: str | None, has_evidence: bool) -> str:
    if nature == "eligibility":
        return "qualification_gap"
    if match_status == "direct_strong":
        return "none"
    allowed = {"none", "expression_gap", "real_gap"}
    if value in allowed:
        return str(value)
    if match_status in {"direct_partial", "transferable_match", "needs_confirmation"} or has_evidence:
        return "expression_gap"
    return "real_gap"


def deep_dive_priority(requirement: dict) -> int:
    if requirement.get("nature") == "eligibility":
        return 70 if requirement.get("eligibility_status") == "needs_confirmation" else 0
    importance = 40 if int(requirement.get("weight", 1)) >= 3 else 20
    proximity = {
        "direct_partial": 35, "transferable_match": 30,
        "needs_confirmation": 20, "no_evidence": 10,
    }.get(requirement.get("match_status"), 0)
    improvement = 25 if requirement.get("gap_type") == "expression_gap" else 0
    return min(100, importance + proximity + improvement)


def _normalize_insight(item: object) -> dict | None:
    if isinstance(item, str):
        return {"title": item, "detail": "", "fact_ids": []}
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or item.get("text") or "").strip()
    detail = str(item.get("detail") or item.get("reason") or "").strip()
    return {"title": title, "detail": detail, "fact_ids": [str(x) for x in item.get("fact_ids", [])]} if title else None


FACT_EXTRACTION_PROMPT = """
你是简历Evidence提取模块。区分Resume明确声明与AI推断，不补全缺失信息。
正式职位必须保持原文，不得根据职责改成 Manager、Supervisor、Team Lead、经理或主管。
每条事实只表达一个主张，并保留逐字出现在Resume中的最短完整source_excerpt。
如果statement只是对source_excerpt的保守同义归纳，evidence_origin=resume_declared。
如果statement包含原文没有明确表达的新能力、范围、因果、权限或解释，evidence_origin=ai_candidate。
公司、正式职位、时间、教育、项目、技能、职责和明确成果都可以是resume_declared，与用户后续确认的补充具有同等User Claim权威。
Employment需要公司和正式职位；Project/Skill/Achievement/Education/Other不要求公司或正式职位，必须按evidence_type判断。
教育必须提取；education中分别保存学历层级和学习形式，不得把开放教育/自考/非全日制误写成没有本科。
严格输出 JSON：
{"facts":[{"evidence_type":"Employment/Project/Education/Achievement/Skill/Other","organization":"","official_job_title":"","project_name":"","role":"","fact_type":"工作身份/实际职责/技能证据/成果/项目/教育/证书/语言/其他","statement":"","skills":[],"source_excerpt":"","evidence_origin":"resume_declared/ai_candidate","education":{"institution":"","education_level":"","major":"","study_type":"unknown","graduation_status":"unknown","degree":"unknown","start_date":"unknown","end_date":"unknown"},"restrictions":""}]}
"""


def extract_resume_evidence(resume_name: str, resume_text: str, user_id: str = database.DEV_USER_ID) -> list[dict]:
    raw = _business_json_call(
        FACT_EXTRACTION_PROMPT, {"resume_name": resume_name, "resume_text": resume_text},
        user_id, "RESUME_INGESTION",
    )
    candidates = []
    seen = set()
    for item in raw.get("facts", []):
        statement = str(item.get("statement", "")).strip()
        organization = str(item.get("organization", "")).strip()
        official_title = str(item.get("official_job_title", "")).strip()
        excerpt = str(item.get("source_excerpt", "")).strip()
        evidence_type = item.get("evidence_type") or _infer_evidence_type_from_fact(str(item.get("fact_type", "")))
        if evidence_type != "Employment":
            organization = official_title = ""
        if not statement or (evidence_type == "Employment" and (not organization or not official_title)) or statement.lower() in seen:
            continue
        requested_origin = item.get("evidence_origin")
        # Only exact excerpts can become user-declared evidence automatically.
        origin = "resume_declared" if requested_origin == "resume_declared" and excerpt and excerpt in resume_text else "ai_candidate"
        seen.add(statement.lower())
        candidates.append(
            {
                "organization": organization,
                "official_job_title": official_title,
                "evidence_type": evidence_type,
                "project_name": str(item.get("project_name", "")).strip(),
                "role": str(item.get("role", "")).strip(),
                "fact_type": str(item.get("fact_type", "其他")),
                "statement": statement,
                "skills": [str(x).strip() for x in item.get("skills", []) if str(x).strip()],
                "source_excerpt": excerpt,
                "restrictions": str(item.get("restrictions", "")).strip(),
                "evidence_origin": origin,
                "evidence_kind": "direct" if origin == "resume_declared" else "candidate",
                "verification_status": "confirmed" if origin == "resume_declared" else "needs_confirmation",
                "education": item.get("education", {}) if str(item.get("fact_type", "")) == "教育" else {},
            }
        )
    return candidates


def _infer_evidence_type_from_fact(fact_type: str) -> str:
    if any(value in fact_type for value in ("项目",)):
        return "Project"
    if any(value in fact_type for value in ("教育", "学历")):
        return "Education"
    if any(value in fact_type for value in ("成果",)):
        return "Achievement"
    if any(value in fact_type for value in ("技能", "证书", "语言")):
        return "Skill"
    if any(value in fact_type for value in ("工作身份", "实际职责")):
        return "Employment"
    return "Other"


def extract_candidate_facts(resume_name: str, resume_text: str, user_id: str = database.DEV_USER_ID) -> list[dict]:
    """Backward-compatible alias used by older callers."""
    return extract_resume_evidence(resume_name, resume_text, user_id=user_id)


PARSER_VERSION = "v011-core-fix-1"


def extract_local_declared_evidence(resume_text: str) -> list[dict]:
    """Deterministic pass for explicit AI/education lines; never infers omitted fields."""
    lines = [line.strip() for line in resume_text.splitlines() if line.strip()]
    result: list[dict] = []
    ai_terms = ("Prompt", "Embedding", "RAG", "向量检索", "向量数据库", "Tool Calling", "Agent", "ChatGPT", "MVP范围", "验收标准")
    for line in lines:
        if any(term.lower() in line.lower() for term in ai_terms):
            is_project = "ChatGPT" in line
            result.append({
                "organization": "天命战棋" if is_project else "职业技能",
                "official_job_title": "AI辅助独立开发" if is_project else "AI应用能力",
                "fact_type": "项目" if is_project else "技能证据",
                "statement": line, "skills": [term for term in ai_terms if term.lower() in line.lower()],
                "source_excerpt": line, "evidence_origin": "resume_declared", "evidence_kind": "direct",
                "verification_status": "confirmed", "restrictions": "仅按Resume原文强度使用，不得升级为正式AI产品经理工作经验。",
            })
    for index, line in enumerate(lines):
        if "｜" not in line or not (
            any(level in line for level in ("本科", "大专", "硕士", "博士"))
            or any(kind in line for kind in ("大学", "学院"))
        ):
            continue
        parts = [part.strip() for part in line.split("｜")]
        institution = parts[0]
        detail = " ".join(parts[1:])
        level = next((value for value in ("博士", "硕士", "本科", "大专") if value in detail), "unknown")
        study_type = next((value for value in ("自考", "开放教育", "非全日制", "全日制", "统招") if value in detail), "unknown")
        status = "在籍" if "在籍" in detail else ("毕业" if "毕业" in detail else "unknown")
        date_line = lines[index + 1] if index + 1 < len(lines) and re.search(r"20\d{2}", lines[index + 1]) else ""
        major = detail.split(level)[0].strip() if level != "unknown" else detail
        statement = line + (f"，{date_line}" if date_line else "")
        result.append({
            "organization": institution, "official_job_title": level, "fact_type": "教育",
            "statement": statement, "skills": [], "source_excerpt": line,
            "evidence_origin": "resume_declared", "evidence_kind": "direct", "verification_status": "confirmed",
            "education": {
                "institution": institution, "education_level": level, "major": major,
                "study_type": study_type, "graduation_status": status,
                "degree": "unknown", "start_date": date_line.split("—")[0] if "—" in date_line else "unknown",
                "end_date": date_line.split("—", 1)[1] if "—" in date_line else "unknown",
            }, "restrictions": "学习形式、毕业状态和Degree仅使用明确字段；unknown不得猜测。",
        })
    return result


def sync_all_resume_evidence(user_id: str, resumes: list[dict], database_module) -> dict:
    """Build career truth from every Resume; Base Resume is intentionally absent."""
    parsed = added = merged = 0
    for resume in resumes:
        for evidence in extract_local_declared_evidence(resume["content_text"]):
            evidence.update({"source": f"简历：{resume['name']}", "source_resume_id": resume["id"]})
            _, created = database_module.add_or_merge_resume_fact(user_id, evidence, resume["id"])
            added += int(created)
            merged += int(not created)
        digest = hashlib.sha256(resume["content_text"].encode("utf-8")).hexdigest()
        if not database_module.resume_needs_ingestion(user_id, resume["id"], digest, PARSER_VERSION):
            continue
        evidence_items = extract_resume_evidence(resume["name"], resume["content_text"], user_id=user_id)
        for evidence in evidence_items:
            evidence.update({"source": f"简历：{resume['name']}", "source_resume_id": resume["id"]})
            if evidence["evidence_origin"] == "resume_declared":
                _, created = database_module.add_or_merge_resume_fact(user_id, evidence, resume["id"])
                added += int(created)
                merged += int(not created)
            else:
                _, created = database_module.add_or_reuse_candidate_fact(user_id, evidence, resume["id"])
                added += int(created)
                merged += int(not created)
        database_module.mark_resume_ingested(user_id, resume["id"], digest, PARSER_VERSION)
        parsed += 1
    return {"parsed_resumes": parsed, "added": added, "merged": merged}


DEEP_DIVE_QUESTION_PROMPT = """
你是“经历深挖”助手。根据一个证据不足的JD Requirement、用户全部Resume和已有Evidence，生成一个简短、具体、开放式中文问题。
不要只是把JD原文改成“你是否做过”。应从用户已有经历中寻找相邻场景或可迁移能力，帮助用户回忆真实案例。
问题必须允许用户说明部分满足、规模差距、没有正式权限、不同岗位/行业等事实，不得诱导用户声称完全满足JD。
严格输出JSON：{"question":"","why_ask":"","related_fact_ids":[]}。
"""


def generate_deep_dive_question(
    requirement: dict, resumes: list[dict], facts: list[dict], user_id: str = database.DEV_USER_ID
) -> dict:
    raw = _business_json_call(
        DEEP_DIVE_QUESTION_PROMPT,
        {
            "requirement": requirement,
            "resumes": [{"id": r["id"], "name": r["name"], "content_text": r["content_text"]} for r in resumes],
            "career_evidence": facts,
        },
        user_id, "DEEP_DIVE",
    )
    valid_ids = {f["id"] for f in facts}
    question = str(raw.get("question", "")).strip()
    if not question:
        question = f"结合你现有的工作或项目经历，请描述一个与“{requirement['text']}”最接近的真实案例；即使只部分相关也可以。"
    return {
        "question": question,
        "why_ask": str(raw.get("why_ask", "")).strip(),
        "related_fact_ids": [fid for fid in raw.get("related_fact_ids", []) if fid in valid_ids],
    }


ANSWER_EXTRACTION_PROMPT = """
你负责把用户对“经历深挖”问题的自由回答整理成一个完整 Experience Case，并在后台拆成原子 Evidence。
只提取用户明确说出的内容。规模、人数、权限、否定事实必须精确保留；不得把“带过8人但没有排班权”写成“管理团队并负责排班”。
Case.summary 是待用户确认的事实上下文，不是最终简历文案，不要写成夸张的求职表达。
用户自由回答是User Claim；用户整体确认Case后，忠实提取的Evidence与Resume Evidence具有同等权威，不需要额外现实证明。
AI只能结构化用户已经声明的内容，不能补充回答中没有的职责、权限、数字、成果、技能、行业经验或因果关系。
不同主张分成不同Evidence，包括明确的限制或没有的权限。所有结果等待用户确认。
evidence_type只能是 Employment/Project/Education/Achievement/Skill/Other。
Project/Skill/Achievement等非Employment Evidence的organization和official_job_title必须为空字符串；业务身份以evidence_type为准。
严格输出JSON：
{"case":{"case_type":"Employment/Project/Education/Achievement/Skill/Other","title":"","summary":"","skills":[],"organization":"","official_job_title":"","project_name":"","role":"","date_range":"","context":""},"evidence":[{"evidence_type":"Employment/Project/Education/Achievement/Skill/Other","organization":"","official_job_title":"","project_name":"","role":"","date_range":"","fact_type":"实际职责/技能证据/成果/限制或负面事实/其他","statement":"","skills":[],"evidence_kind":"direct/transferable/negative","allowed_for_generation":true,"restrictions":""}]}。
负面权限或限制的allowed_for_generation应为false。
"""


def organize_deep_dive_case(
    requirement, question, answer, resumes=None, facts=None, user_id: str = database.DEV_USER_ID
) -> dict:
    raw = _business_json_call(ANSWER_EXTRACTION_PROMPT, {
        "requirement": requirement, "question": question, "user_answer": answer,
        "resume_context": [{"id": r["id"], "name": r["name"], "content_text": r["content_text"]} for r in (resumes or [])],
        "career_evidence": facts or [],
    }, user_id, "DEEP_DIVE")
    case = raw.get("case", {})
    case_type = case.get("case_type") if case.get("case_type") in {"Employment", "Project", "Education", "Achievement", "Skill", "Other"} else "Other"
    case = {key: str(case.get(key, "")).strip() for key in ("title", "summary", "organization", "official_job_title", "project_name", "role", "date_range", "context")} | {
        "case_type": case_type, "skills": [str(x).strip() for x in case.get("skills", []) if str(x).strip()],
    }
    if case_type != "Employment":
        case["organization"] = case["official_job_title"] = ""
    evidence = []
    for item in raw.get("evidence", raw.get("facts", [])):
        statement = str(item.get("statement", "")).strip()
        if not statement:
            continue
        kind = item.get("evidence_kind") if item.get("evidence_kind") in {"direct", "transferable", "negative"} else "direct"
        evidence_type = item.get("evidence_type") or case_type
        if evidence_type not in {"Employment", "Project", "Education", "Achievement", "Skill", "Other"}:
            evidence_type = "Other"
        organization = str(item.get("organization", case.get("organization", ""))).strip() if evidence_type == "Employment" else ""
        official_title = str(item.get("official_job_title", case.get("official_job_title", ""))).strip() if evidence_type == "Employment" else ""
        evidence.append({
            "evidence_type": evidence_type, "organization": organization, "official_job_title": official_title,
            "project_name": str(item.get("project_name", case.get("project_name", ""))).strip(),
            "role": str(item.get("role", case.get("role", ""))).strip(), "date_range": str(item.get("date_range", case.get("date_range", ""))).strip(),
            "fact_type": str(item.get("fact_type", "其他")), "statement": statement,
            "skills": [str(x).strip() for x in item.get("skills", []) if str(x).strip()],
            "evidence_origin": "ai_candidate", "evidence_kind": kind, "verification_status": "needs_confirmation",
            "allowed_for_generation": bool(item.get("allowed_for_generation", True)) and kind != "negative",
            "restrictions": str(item.get("restrictions", "")).strip(), "context": case.get("summary", ""),
        })
    return {"case": case, "evidence": evidence}


def extract_deep_dive_candidates(
    requirement: dict,
    question: str,
    answer: str,
    resumes: list[dict] | None = None,
    facts: list[dict] | None = None,
    user_id: str = database.DEV_USER_ID,
) -> list[dict]:
    return organize_deep_dive_case(requirement, question, answer, resumes, facts, user_id=user_id)["evidence"]


EDIT_PROMPT = """
你是受约束的 Resume Edit Planner。你只能提出编辑指令，不能直接输出或覆盖整份简历。
目标是在真实性优先的前提下，使 Base Resume 更适合 JD。

只允许动作：keep、rewrite、reframe、reorder、remove、de_emphasize、add_from_confirmed_fact。
- rewrite/reframe 必须引用支持改写的已确认 Fact ID。
- add_from_confirmed_fact只能使用career_evidence，Evidence可来自Base Resume、其他Resume或用户确认补充。
- No Evidence / Needs Confirmation 的要求不能新增内容，应保留给 Clarification Flow。
- 不得改变公司、official_job_title、任职时间。
- 不得创造技能、权限、数字、证书、学历、成果或项目。
- original_text 必须逐字复制 Base Resume 中可定位的原文。
- target_text 用于指定插入位置或 reorder 目标；不需要时为空。
- 每项编辑必须关联相关 Requirement ID；纯保留可以没有 Fact ID。
- 严格遵守content_controls：skip禁止使用；must_include必须保留或补入；de_emphasize压缩弱化；include正常使用。
- Transferable Evidence只能表达它实际证明的沟通、协调、培训、执行等能力，不能改写成目标行业的直接经验。
- 遵循polish_strategy和user_preference，但优先级固定为：真实性规则 > content_controls > user_preference > JD Alignment。
- 允许在Evidence语义支持范围内使用更专业、更招聘友好的表达；不得把参与升级为主导、协助升级为负责、使用过升级为专家。
- User Claim包括Resume原文、Deep Dive确认内容和用户手动确认内容，三者权威相同。
- 允许不增加事实的职业语义包装，例如已知IGG工作经历可表达为跨国/海外游戏公司经历；已知根据CTR、下载率和市场反馈调整素材，可表达为基于投放数据反馈优化素材。
- 语义包装不得升级为负责增长、主导市场增长或产生具体提升，除非Evidence明确声明。
- Experience Case.summary只是已确认的事实上下文，不能原样当作最终简历文案；应结合JD做Candidate Positioning后再表达。
- 按polish_strategy.block_ranking处理经历：High优先前置和展开，Medium保留或压缩，Low压缩或删除；但不得违反must_include。
- Base Resume提供最终Draft的结构基础，不代表其中每段都必须保留。
- 可以提出JD导向的职业摘要/Headline，但不得把目标职位写成候选人的正式历史Job Title。

严格输出 JSON：
{"edits":[{"action":"","target_section":"","target_text":"","original_text":"","proposed_text":"","fact_ids":[],"requirement_ids":[],"modification_reason":""}],"summary":"中文简短策略说明"}
"""


def propose_resume_edits(
    base_resume: dict,
    jd_text: str,
    analysis: dict,
    facts: list[dict],
    content_controls: dict | None = None,
    user_preference: str = "",
    polish_strategy: dict | None = None,
    user_id: str = database.DEV_USER_ID,
) -> dict:
    raw = _business_json_call(
        EDIT_PROMPT,
        {
            "base_resume": {
                "id": base_resume["id"],
                "name": base_resume["name"],
                "content_text": base_resume["content_text"],
            },
            "jd": jd_text,
            "requirements": analysis.get("requirements", []),
            "career_evidence": facts,
            "content_controls": content_controls or {},
            "user_preference": user_preference,
            "polish_strategy": polish_strategy or {},
        },
        user_id, "RESUME_EDIT",
    )
    allowed_actions = {
        "keep", "rewrite", "reframe", "reorder", "remove", "de_emphasize", "add_from_confirmed_fact"
    }
    edits = []
    for item in raw.get("edits", []):
        action = item.get("action")
        if action not in allowed_actions:
            continue
        edits.append(
            {
                "action": action,
                "target_section": str(item.get("target_section", "")),
                "target_text": str(item.get("target_text", "")),
                "original_text": str(item.get("original_text", "")),
                "proposed_text": str(item.get("proposed_text", "")),
                "fact_ids": [str(x) for x in item.get("fact_ids", [])],
                "requirement_ids": [str(x) for x in item.get("requirement_ids", [])],
                "modification_reason": str(item.get("modification_reason", "")),
            }
        )
    return {"edits": edits, "summary": str(raw.get("summary", ""))}


STRATEGY_PROMPT = """
你是简历润色策略模块。目标是在Fact Integrity约束下最大化JD Alignment。
只使用career_evidence，不写完整简历，不新增事实。结合Base Resume、JD、Requirement、内容控制和用户本次要求，输出非常简短的中文策略。
必须明确区分：应该突出什么、适当弱化什么、绝不包装成什么、建议先补充什么。
Transferable Evidence不能包装成Direct Experience；个人项目不能包装成正式工作经历。
必须对输入的experience_blocks逐一给出JD相关性High/Medium/Low、定位角度、建议动作和顺序。
High应前置/展开，Medium保留/压缩，Low压缩/删除；must_include仍必须保留。
Experience Case.summary只是事实上下文，不是可直接复制的最终Resume文案。
优先级：真实性规则 > Skip/Must Include > 用户本次要求 > JD Alignment > 默认策略。
严格输出JSON：
{"highlight":[],"de_emphasize":[],"never_claim":[],"suggest_clarification":[],"headline":"","section_order":[],"block_ranking":[{"block_id":"","relevance":"High/Medium/Low","reason":"","positioning":"","recommended_action":"expand/keep/compress/remove","recommended_order":1}],"summary":""}
"""


def generate_polish_strategy(
    base_resume: dict,
    jd_text: str,
    analysis: dict,
    facts: list[dict],
    content_controls: dict,
    user_preference: str = "",
    experience_blocks: list[dict] | None = None,
    user_id: str = database.DEV_USER_ID,
) -> dict:
    raw = _business_json_call(
        STRATEGY_PROMPT,
        {
            "base_resume": {"id": base_resume["id"], "name": base_resume["name"], "content_text": base_resume["content_text"]},
            "jd": jd_text,
            "requirements": analysis.get("requirements", []),
            "career_evidence": facts,
            "content_controls": content_controls,
            "user_preference": user_preference,
            "experience_blocks": experience_blocks or [],
        },
        user_id, "STRATEGY",
    )
    def clean_list(value: object) -> list[str]:
        if isinstance(value, str):
            lines = [line.strip(" -•\t") for line in value.splitlines() if line.strip(" -•\t")]
            return (lines or [value.strip()])[:6]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()][:6]
        return []

    valid_block_ids = {block.get("id") for block in (experience_blocks or [])}
    ranking = []
    for item in raw.get("block_ranking", []):
        if item.get("block_id") not in valid_block_ids:
            continue
        relevance = str(item.get("relevance", "Medium")).title()
        action = str(item.get("recommended_action", "keep")).lower()
        ranking.append({
            "block_id": item["block_id"], "relevance": relevance if relevance in {"High", "Medium", "Low"} else "Medium",
            "reason": str(item.get("reason", "")).strip(), "positioning": str(item.get("positioning", "")).strip(),
            "recommended_action": action if action in {"expand", "keep", "compress", "remove"} else "keep",
            "recommended_order": int(item.get("recommended_order", 999)) if str(item.get("recommended_order", "")).isdigit() else 999,
        })
    return {
        key: clean_list(raw.get(key, []))
        for key in ("highlight", "de_emphasize", "never_claim", "suggest_clarification")
    } | {"headline": str(raw.get("headline", "")).strip(), "section_order": clean_list(raw.get("section_order", [])),
         "block_ranking": sorted(ranking, key=lambda item: item["recommended_order"]), "summary": str(raw.get("summary", "")).strip()}


RESUME_PROMPT = """
你是谨慎的简历编辑。根据目标 JD、结构化要求和已确认职业事实生成针对性 Resume Draft。
简历语言应与 JD 和岗位招聘语言一致；不确定时使用 JD 的主要语言。

最高规则：只能使用 confirmed_career_facts。不得创造或推断工作经历、正式职位、职责、技能、项目、成果数字、证书、学历或管理权限。不得修改 official_job_title。缺少依据就不写。
每一条 summary 或 bullet 都必须列出支持它的 fact_ids，且只能引用输入中存在的 Fact ID。
不要输出 Match Score。不要声称这是最终简历。

输出 JSON：
{
  "target_title": "...",
  "language": "zh 或 en",
  "summary": {"text": "...", "fact_ids": ["CF-..."]},
  "experiences": [
    {"organization": "必须与事实一致", "official_job_title": "必须与事实一致", "bullets": [{"text": "...", "fact_ids": ["CF-..."]}]}
  ],
  "skills": [{"text": "...", "fact_ids": ["CF-..."]}],
  "warnings": ["..."]
}
"""


def generate_resume(
    jd_text: str, analysis: dict, facts: list[dict], user_id: str = database.DEV_USER_ID
) -> dict:
    raw = _business_json_call(
        RESUME_PROMPT,
        {"jd": jd_text, "analysis": analysis, "confirmed_career_facts": facts},
        user_id, "RESUME_EDIT",
    )
    valid_ids = {f["id"] for f in facts if f.get("verification_status") == "confirmed"}
    fact_by_id = {f["id"]: f for f in facts}

    def clean_refs(item: dict) -> list[str]:
        return [fid for fid in item.get("fact_ids", []) if fid in valid_ids]

    summary = raw.get("summary", {}) if isinstance(raw.get("summary"), dict) else {}
    summary["fact_ids"] = clean_refs(summary)
    if summary.get("text") and not summary["fact_ids"]:
        summary = {"text": "", "fact_ids": []}

    experiences = []
    for exp in raw.get("experiences", []):
        bullets = []
        for bullet in exp.get("bullets", []):
            refs = clean_refs(bullet)
            if bullet.get("text") and refs:
                bullets.append({"text": str(bullet["text"]), "fact_ids": refs})
        if not bullets:
            continue
        referenced = [fact_by_id[fid] for b in bullets for fid in b["fact_ids"]]
        allowed_orgs = {f["organization"] for f in referenced}
        allowed_titles = {f["official_job_title"] for f in referenced}
        organization = str(exp.get("organization", ""))
        title = str(exp.get("official_job_title", ""))
        if organization not in allowed_orgs or title not in allowed_titles:
            organization = referenced[0]["organization"]
            title = referenced[0]["official_job_title"]
        experiences.append({"organization": organization, "official_job_title": title, "bullets": bullets})

    skills = []
    for skill in raw.get("skills", []):
        refs = clean_refs(skill)
        if skill.get("text") and refs:
            skills.append({"text": str(skill["text"]), "fact_ids": refs})

    return {
        "target_title": str(raw.get("target_title", "")),
        "language": raw.get("language", "zh"),
        "summary": summary,
        "experiences": experiences,
        "skills": skills,
        "warnings": [str(w) for w in raw.get("warnings", [])],
        "status": "draft",
    }
