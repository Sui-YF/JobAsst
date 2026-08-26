"""Deterministic scoring rules for Demo V0.1.

The model may suggest requirement types and match states, but it never computes
the score. This module intentionally contains no AI calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


STATUS_FACTORS = {
    "direct_strong": 1.0,
    "direct_partial": 0.65,
    "transferable_match": 0.45,
    # Backward-compatible V0.1 aliases for saved analyses.
    "strong_match": 1.0,
    "partial_match": 0.65,
    "no_evidence": 0.0,
    "not_met": 0.0,
    "needs_confirmation": 0.0,
}

EVIDENCE_COMPLETE_STATES = {
    "direct_strong", "direct_partial", "transferable_match",
    "strong_match", "partial_match", "not_met",
}


def requirement_weight(priority: str, explicit_critical: bool = False) -> int:
    """Return the approved V0.1 weight. Weight 4 needs explicit JD evidence."""
    if explicit_critical:
        return 4
    if priority in {"must_have", "core_responsibility"}:
        return 3
    return 1


@dataclass(frozen=True)
class ScoreResult:
    match_score: int
    evidence_coverage: int
    earned_points: float
    total_weight: int


def calculate_scores(requirements: Iterable[dict]) -> ScoreResult:
    scored = [r for r in requirements if r.get("nature") == "match"]
    total_weight = sum(int(r["weight"]) for r in scored)
    if total_weight == 0:
        return ScoreResult(0, 0, 0.0, 0)

    earned = sum(
        int(r["weight"]) * STATUS_FACTORS.get(r.get("match_status", ""), 0.0)
        for r in scored
    )
    covered = sum(
        int(r["weight"])
        for r in scored
        if r.get("match_status") in EVIDENCE_COMPLETE_STATES
    )
    quality = earned / total_weight
    coverage = covered / total_weight
    # V0.1.1 calibrated fit score: evidence quality dominates, coverage explains
    # confidence, and 90 is the intentional ceiling. It is not a probability.
    fit_score = round(10 + 70 * quality + 10 * coverage)
    return ScoreResult(
        match_score=fit_score,
        evidence_coverage=round(coverage * 100),
        earned_points=earned,
        total_weight=total_weight,
    )


def calculate_eligibility(requirements: Iterable[dict]) -> str:
    statuses = [
        r.get("eligibility_status", "needs_confirmation")
        for r in requirements
        if r.get("nature") == "eligibility"
    ]
    if not statuses:
        return "未发现明确硬性资格项"
    if "not_met" in statuses:
        return "不符合（存在明确硬性条件风险）"
    if "needs_confirmation" in statuses:
        return "待确认"
    return "符合"


def analysis_delta(before: dict, after: dict) -> dict:
    old = {item["id"]: item for item in before.get("requirements", [])}
    changed = []
    for item in after.get("requirements", []):
        previous = old.get(item["id"], {})
        old_state = previous.get("match_status") or previous.get("eligibility_status")
        new_state = item.get("match_status") or item.get("eligibility_status")
        if old_state != new_state:
            changed.append({"id": item["id"], "text": item.get("text", ""), "before": old_state, "after": new_state})
    old_score, new_score = calculate_scores(before.get("requirements", [])), calculate_scores(after.get("requirements", []))
    return {"score_before": old_score.match_score, "score_after": new_score.match_score,
            "coverage_before": old_score.evidence_coverage, "coverage_after": new_score.evidence_coverage,
            "changed": changed}


def application_advice(requirements: Iterable[dict], score: ScoreResult, eligibility: str) -> tuple[str, str]:
    items = list(requirements)
    if eligibility.startswith("不符合"):
        return "挑战较大", "存在明确硬资格阻断项，润色不能解决。"
    direct = sum(
        int(item.get("weight", 1)) for item in items
        if item.get("nature") == "match" and item.get("match_status") in {"direct_strong", "direct_partial"}
    )
    transferable = any(item.get("match_status") == "transferable_match" for item in items)
    important_real_gaps = any(
        item.get("nature") == "match" and int(item.get("weight", 1)) >= 3 and item.get("gap_type") == "real_gap"
        for item in items
    )
    if score.match_score >= 70 and direct > 0 and not important_real_gaps and eligibility != "待确认":
        return "值得申请", "已有直接证据覆盖核心要求，且未发现明确硬资格风险。"
    if score.match_score >= 45 and (direct > 0 or transferable):
        return "可以尝试", "存在部分直接或可迁移证据；建议先补足表达缺口并人工检查。"
    return "挑战较大", "核心要求的直接证据较弱或存在重要真实缺口。"
