from __future__ import annotations


ACTION_LABELS = {
    "add_from_confirmed_fact": "Added", "rewrite": "Rewritten", "reframe": "Reframed",
    "reorder": "Moved", "de_emphasize": "Compressed", "remove": "Removed", "keep": "Kept",
}


def build_review_items(draft: dict, requirements: list[dict], facts: list[dict], debug: bool = False) -> list[dict]:
    requirement_map = {r["id"]: r for r in requirements}
    fact_map = {f["id"]: f for f in facts}
    result = []
    for edit in draft.get("accepted_edits", draft.get("applied_edits", [])):
        item = {
            "change_type": ACTION_LABELS.get(edit.get("action"), edit.get("action", "Changed")),
            "before": edit.get("original_text", ""), "after": edit.get("proposed_text", ""),
            "why": edit.get("modification_reason", ""),
            "requirements": [requirement_map[rid].get("text", "") for rid in edit.get("requirement_ids", []) if rid in requirement_map],
            "evidence": [fact_map[fid].get("statement", "") for fid in edit.get("fact_ids", []) if fid in fact_map],
        }
        if debug:
            item["fact_ids"] = edit.get("fact_ids", [])
            item["requirement_ids"] = edit.get("requirement_ids", [])
        result.append(item)
    return result
