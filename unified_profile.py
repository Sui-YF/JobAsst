from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher


_NUMBER = re.compile(r"\d+(?:\.\d+)?%?")
_SPACE_PUNCT = re.compile(r"[\s，。；、,.!！:：()（）/|·《》\-]+")


def _norm(value: str) -> str:
    text = _SPACE_PUNCT.sub("", (value or "").lower())
    replacements = {
        "参与": "", "负责": "", "完成": "", "相关工作": "工作",
        "新员工": "新人", "熟悉工作流程": "培训流程", "带教": "培训",
        "unrealengine": "ue", "cinematicdesigner": "cinematic",
        "修仙对抗外星人": "修仙对抗外星",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _category(fact: dict) -> str:
    explicit = fact.get("evidence_type", "").strip()
    if explicit:
        return "Work" if explicit == "Employment" else explicit
    value = f"{fact.get('fact_type', '')} {fact.get('statement', '')}".lower()
    if any(x in value for x in ("教育", "学历", "本科", "大专", "硕士", "学院", "大学")):
        return "Education"
    if any(x in value for x in ("项目", "demo", "capstone", "战棋", "修仙")):
        return "Project"
    if any(x in value for x in ("技能", "证书", "语言")):
        return "Skill"
    return "Work"


def _block_identity(fact: dict) -> tuple[str, str, str]:
    category = _category(fact)
    if category == "Education":
        return category, "education", ""
    if category == "Project":
        # organization is only a legacy project-name fallback; category/identity semantics come from evidence_type.
        return category, _norm(fact.get("project_name") or fact.get("organization") or fact.get("context") or "project"), ""
    if category in {"Skill", "Achievement", "Other"}:
        return category, _norm(fact.get("case_id") or category), ""
    org = _norm(fact.get("organization", ""))
    title = _norm(fact.get("official_job_title", ""))
    if org and org not in {"职业技能", "核心技能", "技能"}:
        return "Experience", org, ""
    return category, org or "unknown", title


def _safe_semantic_match(left: dict, right: dict) -> bool:
    """Conservative display dedup: values with conflicting numbers never merge."""
    a, b = left.get("statement", ""), right.get("statement", "")
    if set(_NUMBER.findall(a)) != set(_NUMBER.findall(b)):
        return False
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return min(len(na), len(nb)) >= 10
    return SequenceMatcher(None, na, nb).ratio() >= 0.72


def build_experience_blocks(facts: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for fact in facts:
        grouped.setdefault(_block_identity(fact), []).append(fact)
    blocks = []
    for identity, block_facts in grouped.items():
        canonical = []
        for fact in block_facts:
            matched = next((item for item in canonical if _safe_semantic_match(item["fact"], fact)), None)
            if matched:
                matched["fact_ids"].append(fact["id"])
                matched["sources"].extend(fact.get("sources", []))
                if len(fact.get("statement", "")) > len(matched["fact"].get("statement", "")):
                    matched["unique_details"].append(fact["statement"])
            else:
                canonical.append({
                    "fact": fact, "fact_ids": [fact["id"]],
                    "sources": list(fact.get("sources", [])), "unique_details": [],
                })
        identity_category, _, _ = identity
        if identity_category == "Education":
            category = "Education"
        elif identity_category == "Experience":
            combined = " ".join(
                f"{fact.get('organization', '')} {fact.get('official_job_title', '')} {fact.get('fact_type', '')}"
                for fact in block_facts
            ).lower()
            category = "Project" if any(x in combined for x in ("demo", "项目", "capstone", "战棋", "修仙")) else "Work"
        else:
            category = identity_category
        sample = block_facts[0]
        titles = {f.get("official_job_title", "").strip() for f in block_facts if f.get("official_job_title", "").strip()}
        if category == "Education":
            heading = "Education"
        elif category == "Project":
            heading = sample.get("project_name") or sample.get("organization") or sample.get("context") or "项目经历"
        elif category in {"Skill", "Achievement", "Other"}:
            heading = {"Skill": "技能", "Achievement": "成果", "Other": "其他经历"}[category]
        else:
            heading = sample.get("organization", "")
        if category == "Work" and len(titles) == 1:
            heading += " · " + next(iter(titles))
        raw_id = "|".join(identity)
        blocks.append({
            "id": hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:10],
            "category": category, "heading": heading or category,
            "evidence": canonical, "raw_fact_count": len(block_facts),
        })
    order = {"Work": 0, "Project": 1, "Achievement": 2, "Education": 3, "Skill": 4, "Other": 5}
    return sorted(blocks, key=lambda b: (order.get(b["category"], 9), b["heading"].lower()))


def resolved_fact_controls(blocks: list[dict], controls: dict) -> dict[str, str]:
    result = {}
    for block in blocks:
        block_value = controls.get(f"block:{block['id']}", "include")
        for evidence in block["evidence"]:
            override = controls.get(f"evidence:{evidence['fact_ids'][0]}")
            for fact_id in evidence["fact_ids"]:
                result[fact_id] = override or block_value
    return result


def canonical_evidence_pool(facts: list[dict]) -> list[dict]:
    """One semantic Evidence item participates once in matching, with all sources retained."""
    pool = []
    for block in build_experience_blocks(facts):
        for evidence in block["evidence"]:
            item = dict(evidence["fact"])
            item["sources"] = evidence["sources"]
            item["duplicate_fact_ids"] = evidence["fact_ids"][1:]
            item["experience_block_id"] = block["id"]
            item["experience_block"] = block["heading"]
            pool.append(item)
    return pool


def profile_summary(resumes: list[dict], facts: list[dict]) -> dict:
    blocks = build_experience_blocks(facts)
    return {
        "Experiences": sum(b["category"] == "Work" for b in blocks),
        "Education": sum(b["category"] == "Education" for b in blocks),
        "Projects": sum(b["category"] == "Project" for b in blocks),
        "Skills": sum(b["category"] == "Skill" for b in blocks),
        "Canonical Evidence": sum(len(b["evidence"]) for b in blocks),
        "Raw Evidence": len(facts),
        "Resume Sources": len(resumes),
        "Conflicts": sum("潜在冲突" in f.get("restrictions", "") for f in facts),
    }
