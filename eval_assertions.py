from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from deepseek_client import _evidence_signature, stabilize_incremental_analysis
from resume_editor import validate_and_apply_edits


MATCHED = {"direct_strong", "direct_partial", "transferable_match"}
VALID_STATUS = MATCHED | {"no_evidence", "not_met", "needs_confirmation"}
VALID_RELEVANCE = {"High", "Medium", "Low"}


@dataclass
class EvalCheck:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _check(checks: list[EvalCheck], name: str, condition: bool, detail: str = "") -> None:
    checks.append(EvalCheck(name, bool(condition), detail if not condition else ""))


def evaluate_fixture(fixture: dict, baseline: dict) -> list[EvalCheck]:
    checks: list[EvalCheck] = []
    evidence = fixture["input"]["career_evidence"]
    evidence_map = {item["id"]: item for item in evidence}
    analysis = baseline["outputs"]["analysis"]
    requirements = analysis.get("requirements", [])

    _check(checks, "fixture_is_versioned", fixture.get("fixture_version", 0) >= 1)
    _check(checks, "fixture_is_locked", fixture.get("locked") is True)
    _check(checks, "baseline_matches_fixture_version",
           baseline.get("fixture_id") == fixture.get("fixture_id") and
           baseline.get("fixture_version") == fixture.get("fixture_version"))
    _check(checks, "requirement_ids_unique", len({r.get("id") for r in requirements}) == len(requirements))
    _check(checks, "match_status_enum", all(r.get("match_status") in VALID_STATUS for r in requirements))
    _check(checks, "cited_evidence_exists",
           all(fid in evidence_map for r in requirements for fid in r.get("fact_ids", [])))
    _check(checks, "cited_evidence_is_confirmed",
           all(evidence_map[fid].get("verification_status") == "confirmed"
               for r in requirements for fid in r.get("fact_ids", []) if fid in evidence_map))
    _check(checks, "cited_evidence_allowed",
           all(evidence_map[fid].get("allowed_for_generation", True)
               for r in requirements for fid in r.get("fact_ids", []) if fid in evidence_map))
    _check(checks, "matched_status_has_evidence",
           all(bool(r.get("fact_ids")) for r in requirements if r.get("match_status") in MATCHED))
    _check(checks, "no_evidence_has_no_fact_ids",
           all(not r.get("fact_ids") for r in requirements if r.get("match_status") == "no_evidence"))
    _check(checks, "not_met_requires_negative_claim",
           all(any(evidence_map.get(fid, {}).get("evidence_kind") == "negative" for fid in r.get("fact_ids", []))
               for r in requirements if r.get("match_status") == "not_met"))

    origins = {item.get("evidence_origin") for item in evidence}
    expected_origins = {"resume_declared", "deep_dive_confirmed", "manual_confirmed"}
    _check(checks, "user_claim_origins_are_represented", expected_origins <= origins)
    authority_ids = baseline["outputs"].get("authority_probe_fact_ids", [])
    cited_origins = {evidence_map[fid].get("evidence_origin") for fid in authority_ids if fid in evidence_map}
    _check(checks, "user_claim_origins_can_all_be_cited", expected_origins <= cited_origins)

    rematch = baseline["outputs"].get("rematch", {})
    if rematch:
        current_map = {fact["id"]: fact for fact in rematch["current_facts"]}
        for requirement in rematch["before"].get("requirements", []):
            requirement.setdefault("evidence_signatures", {
                fid: _evidence_signature(current_map[fid]) for fid in requirement.get("fact_ids", []) if fid in current_map
            })
        stabilized = stabilize_incremental_analysis(rematch["before"], rematch["after"], rematch["current_facts"])
        actual = {r["id"]: r["match_status"] for r in stabilized["requirements"]}
        _check(checks, "unchanged_evidence_does_not_downgrade",
               all(actual.get(rid) == status for rid, status in rematch.get("expected_status", {}).items()))
        _check(checks, "new_evidence_can_upgrade",
               all(actual.get(rid) == status for rid, status in rematch.get("expected_upgrade", {}).items()))
    changed = baseline["outputs"].get("changed_evidence_rematch", {})
    if changed:
        original_map = {fact["id"]: fact for fact in changed["original_facts"]}
        for requirement in changed["before"].get("requirements", []):
            requirement["evidence_signatures"] = {
                fid: _evidence_signature(original_map[fid]) for fid in requirement.get("fact_ids", []) if fid in original_map
            }
        changed_result = stabilize_incremental_analysis(
            changed["before"], changed["after"], changed["current_facts"]
        )
        changed_actual = {r["id"]: r["match_status"] for r in changed_result["requirements"]}
        _check(checks, "changed_evidence_allows_downgrade",
               all(changed_actual.get(rid) == status for rid, status in changed["expected_status"].items()))

    ranking = baseline["outputs"].get("block_ranking", [])
    block_ids = {b["id"] for b in fixture["input"].get("experience_blocks", [])}
    _check(checks, "ranking_uses_known_blocks", all(r.get("block_id") in block_ids for r in ranking))
    _check(checks, "ranking_relevance_enum", all(r.get("relevance") in VALID_RELEVANCE for r in ranking))
    order = {"High": 0, "Medium": 1, "Low": 2}
    _check(checks, "high_medium_low_order", [order[r["relevance"]] for r in ranking] ==
           sorted(order[r["relevance"]] for r in ranking))
    _check(checks, "ranking_actions_follow_relevance", all(
        r.get("recommended_action") in ({"expand", "keep"} if r["relevance"] == "High" else
                                        {"keep", "compress"} if r["relevance"] == "Medium" else
                                        {"compress", "remove"}) for r in ranking))

    edit_cases = baseline["outputs"].get("guardrail_cases", [])
    accepted = rejected = 0
    for case in edit_cases:
        result = validate_and_apply_edits(
            fixture["input"]["base_resume"]["content_text"], [case["edit"]], evidence,
            requirements, fixture["input"].get("content_controls", {}),
            fixture["input"]["base_resume"]["id"],
        )
        if case["expected"] == "accept":
            accepted += int(bool(result.applied))
        else:
            rejected += int(bool(result.rejected))
    _check(checks, "professional_reframing_is_accepted",
           accepted == sum(c["expected"] == "accept" for c in edit_cases))
    _check(checks, "fact_upgrades_are_rejected",
           rejected == sum(c["expected"] == "reject" for c in edit_cases))

    draft = baseline["outputs"].get("draft_text", "")
    controls = fixture["input"].get("content_controls", {})
    skipped = [evidence_map[fid]["statement"] for fid, value in controls.items() if value == "skip" and fid in evidence_map]
    required = [evidence_map[fid]["statement"] for fid, value in controls.items() if value == "must_include" and fid in evidence_map]
    _check(checks, "skip_has_priority", all(text not in draft for text in skipped))
    _check(checks, "must_include_has_priority", all(text in draft for text in required))
    _check(checks, "draft_is_not_base_resume_copy", draft != fixture["input"]["base_resume"]["content_text"])
    return checks
