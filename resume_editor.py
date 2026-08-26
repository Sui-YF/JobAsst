"""Validated Resume Edit Layer.

DeepSeek proposes operations. This module validates references and applies only
the supported subset to a Base Resume. It never calls an AI model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


ALLOWED_ACTIONS = {
    "keep",
    "rewrite",
    "reframe",
    "reorder",
    "remove",
    "de_emphasize",
    "add_from_confirmed_fact",
}
MUTATING_TEXT_ACTIONS = {"rewrite", "reframe", "remove", "de_emphasize", "add_from_confirmed_fact"}
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?%?")
DATE_PATTERN = re.compile(
    r"(?:19|20)\d{2}|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b",
    re.IGNORECASE,
)
RISKY_TITLE_PATTERN = re.compile(r"\b(?:manager|supervisor|team lead|director|负责人|经理|主管)\b", re.IGNORECASE)
ENGLISH_WORD = re.compile(r"[a-zA-Z][a-zA-Z+.#-]*")
CHINESE_CHAR = re.compile(r"[\u4e00-\u9fff]")

SEMANTIC_SYNONYMS = {
    "communicated": "explain", "communicate": "explain", "explained": "explain",
    "requirements": "rule", "requirement": "rule", "rules": "rule",
    "procedures": "rule", "procedure": "rule", "employees": "employee",
    "hires": "hire", "helped": "help", "chinese-speaking": "chinese",
    "新人": "新员工", "带教": "培训", "传达": "解释", "规范": "要求",
}
ENGLISH_STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with",
    "speaking", "new",
}
HIGH_RISK_EXPANSIONS = {
    "管理", "领导", "主导", "负责人", "绩效考核", "绩效管理", "排班", "招聘", "审批", "决策权", "预算权",
    "经理", "主管", "团队管理", "独立负责", "独立完成", "从0到1", "战略", "路线图",
    "提升", "降低", "增长", "节省", "转化率", "产出", "实现了", "推动落地",
    "多年经验", "丰富经验", "资深", "行业经验", "产品经理经验", "管理经验",
    "python", "java", "c++", "sql", "aws", "azure", "rag", "llm", "agent", "blueprint", "unreal", "ue5",
}


@dataclass
class EditExecutionResult:
    content: str
    applied: list[dict]
    rejected: list[dict]
    control_notes: list[str] = field(default_factory=list)


def _reject(edit: dict, reason: str) -> dict:
    return {"edit": edit, "reason": reason}


def _semantic_normalize(text: str) -> str:
    value = text.lower()
    for old, new in SEMANTIC_SYNONYMS.items():
        value = value.replace(old, new)
    return value


def _english_concepts(text: str) -> set[str]:
    result = set()
    for token in ENGLISH_WORD.findall(_semantic_normalize(text)):
        token = token.strip("-.+")
        if token and token not in ENGLISH_STOPWORDS:
            result.add(token)
    return result


def _chinese_bigrams(text: str) -> set[str]:
    chars = "".join(CHINESE_CHAR.findall(_semantic_normalize(text)))
    return {chars[i:i + 2] for i in range(len(chars) - 1)}


def _semantic_support_error(proposed: str, original: str, cited_facts: list[dict]) -> str | None:
    support = " ".join(
        [original]
        + [fact.get("statement", "") for fact in cited_facts]
        + [fact.get("context", "") for fact in cited_facts]
        + [" ".join(fact.get("skills", [])) for fact in cited_facts]
        + [fact.get("organization", "") for fact in cited_facts]
        + [fact.get("official_job_title", "") for fact in cited_facts]
    )
    # Controlled wording aliases add professional context, never duties, outcomes, authority or metrics.
    if any(fact.get("organization", "").strip().lower() == "igg" for fact in cited_facts):
        support += " 跨国游戏公司工作经历 海外游戏公司工作经历"
    claim_text = " ".join(fact.get("statement", "") + " " + fact.get("context", "") for fact in cited_facts).lower()
    if (("ctr" in claim_text or "点击率" in claim_text) and
            ("下载率" in claim_text or "download" in claim_text) and
            any(term in claim_text for term in ("反馈", "调整", "优化"))):
        support += " 基于投放数据反馈持续优化素材 根据点击率下载率市场反馈调整素材"
    normalized_proposed = _semantic_normalize(proposed)
    normalized_support = _semantic_normalize(support)

    unsupported_risks = sorted(
        term for term in HIGH_RISK_EXPANSIONS
        if term in normalized_proposed and term not in normalized_support
    )
    if unsupported_risks:
        return "拟议文本扩展了事实未支持的技能、职责、权限、经验或成果：" + "、".join(unsupported_risks)

    proposed_english = _english_concepts(proposed)
    support_english = _english_concepts(support)
    if proposed_english:
        english_coverage = len(proposed_english & support_english) / len(proposed_english)
        if english_coverage < 0.65:
            unsupported = sorted(proposed_english - support_english)
            return "拟议文本包含无法从引用事实追溯的英文概念：" + "、".join(unsupported)

    proposed_zh = _chinese_bigrams(proposed)
    support_zh = _chinese_bigrams(support)
    if len(proposed_zh) >= 4:
        zh_coverage = len(proposed_zh & support_zh) / len(proposed_zh)
        if zh_coverage < 0.45:
            return "拟议文本与引用事实的中文语义覆盖不足，需拒绝或由用户确认"
    return None


def validate_and_apply_edits(
    base_text: str,
    edits: list[dict],
    confirmed_facts: list[dict],
    requirements: list[dict],
    content_controls: dict | None = None,
    base_resume_id: str | None = None,
) -> EditExecutionResult:
    controls = content_controls or {}
    fact_map = {
        fact["id"]: fact
        for fact in confirmed_facts
        if fact.get("verification_status") == "confirmed"
        and fact.get("source_valid", 1) == 1
        and fact.get("allowed_for_generation", 1) == 1
    }
    requirement_ids = {req["id"] for req in requirements}
    protected_identity = {
        value.strip()
        for fact in confirmed_facts
        for value in (fact.get("organization", ""), fact.get("official_job_title", ""))
        if value.strip() and value.strip().lower() in base_text.lower()
    }
    content = base_text
    applied: list[dict] = []
    rejected: list[dict] = []

    for raw_edit in edits:
        edit = dict(raw_edit)
        action = edit.get("action")
        original = str(edit.get("original_text", "")).strip()
        target = str(edit.get("target_text", "")).strip()
        proposed = str(edit.get("proposed_text", "")).strip()
        fact_ids = list(dict.fromkeys(edit.get("fact_ids", [])))
        req_ids = list(dict.fromkeys(edit.get("requirement_ids", [])))

        if action not in ALLOWED_ACTIONS:
            rejected.append(_reject(edit, "不支持的编辑动作"))
            continue
        if any(fid not in fact_map for fid in fact_ids):
            rejected.append(_reject(edit, "引用了不存在或未确认的 Fact ID"))
            continue
        if any(controls.get(fid) == "skip" for fid in fact_ids):
            rejected.append(_reject(edit, "引用了本次申请中标记为Skip的Evidence"))
            continue
        if any(rid not in requirement_ids for rid in req_ids):
            rejected.append(_reject(edit, "引用了不存在的 Requirement ID"))
            continue
        if action in MUTATING_TEXT_ACTIONS and not fact_ids:
            rejected.append(_reject(edit, "新增或改写文本缺少已确认 Fact ID"))
            continue
        if action in {"rewrite", "reframe", "remove", "de_emphasize", "reorder"} and (not original or original not in content):
            rejected.append(_reject(edit, "无法在 Base Resume 中定位 original text"))
            continue
        if action in {"rewrite", "reframe", "remove", "de_emphasize"}:
            identities_in_original = [identity for identity in protected_identity if identity.lower() in original.lower()]
            dates_in_original = set(DATE_PATTERN.findall(original))
            if action != "remove" and (identities_in_original or dates_in_original):
                preserves_identities = all(
                    identity.lower() in proposed.lower()
                    or (identity.lower() == "igg" and any(label in proposed for label in ("跨国游戏公司", "海外游戏公司")))
                    for identity in identities_in_original
                )
                preserves_dates = dates_in_original.issubset(set(DATE_PATTERN.findall(proposed)))
                if not proposed or not preserves_identities or not preserves_dates:
                    rejected.append(_reject(edit, "拟议文本没有完整保留公司、正式职位或任职时间"))
                    continue
        if proposed and RISKY_TITLE_PATTERN.search(proposed) and not any(
            RISKY_TITLE_PATTERN.search(fact.get("statement", ""))
            or RISKY_TITLE_PATTERN.search(fact.get("official_job_title", ""))
            for fid, fact in fact_map.items() if fid in fact_ids
        ):
            rejected.append(_reject(edit, "拟议文本包含没有事实支持的管理头衔或权限"))
            continue
        if proposed:
            supported_text = " ".join(
                [original]
                + [fact_map[fid].get("statement", "") for fid in fact_ids]
                + [" ".join(fact_map[fid].get("skills", [])) for fid in fact_ids]
            )
            unsupported_numbers = set(NUMBER_PATTERN.findall(proposed)) - set(NUMBER_PATTERN.findall(supported_text))
            if unsupported_numbers:
                rejected.append(_reject(edit, "拟议文本引入了未经事实支持的数字"))
                continue
            semantic_error = _semantic_support_error(proposed, original, [fact_map[fid] for fid in fact_ids])
            if semantic_error:
                rejected.append(_reject(edit, semantic_error))
                continue

        if action == "keep":
            applied.append(edit)
            continue
        if action in {"rewrite", "reframe", "de_emphasize"}:
            if not proposed:
                rejected.append(_reject(edit, "改写操作缺少 proposed text"))
                continue
            content = content.replace(original, proposed, 1)
        elif action == "remove":
            content = content.replace(original, "", 1)
        elif action == "add_from_confirmed_fact":
            if not proposed:
                rejected.append(_reject(edit, "新增操作缺少 proposed text"))
                continue
            if target and target in content:
                content = content.replace(target, target + "\n" + proposed, 1)
            else:
                content = content.rstrip() + "\n" + proposed
        elif action == "reorder":
            if not target or target not in content or target == original:
                rejected.append(_reject(edit, "reorder 缺少有效目标位置"))
                continue
            without = content.replace(original, "", 1)
            content = without.replace(target, original + "\n" + target, 1)
        applied.append(edit)

    control_notes: list[str] = []
    for fact_id, choice in controls.items():
        fact = fact_map.get(fact_id)
        if not fact:
            continue
        excerpts = [
            source.get("source_excerpt", "").strip()
            for source in fact.get("sources", [])
            if source.get("source_excerpt") and (not base_resume_id or source.get("resume_id") == base_resume_id)
        ]
        statement = fact.get("statement", "").strip()
        if choice == "skip":
            for excerpt in excerpts + [statement]:
                if excerpt and excerpt in content and not DATE_PATTERN.search(excerpt) and not any(
                    identity.lower() in excerpt.lower() for identity in protected_identity
                ):
                    content = content.replace(excerpt, "", 1)
                    control_notes.append(f"已按Skip移除：{fact_id}")
        elif choice == "must_include":
            already_traced = any(fact_id in edit.get("fact_ids", []) for edit in applied)
            already_present = any(value and value in content for value in excerpts + [statement])
            if not already_traced and not already_present and statement:
                content = content.rstrip() + "\n" + statement
                control_notes.append(f"Edit Planner遗漏Must Include，程序按已确认事实补入：{fact_id}")

    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    return EditExecutionResult(content=content, applied=applied, rejected=rejected, control_notes=control_notes)
