from resume_editor import validate_and_apply_edits


BASE = "Demo Warehouse\nWarehouse Associate\n2022 - 2025\nExplained safety rules to Chinese employees."
FACTS = [
    {
        "id": "CF-1",
        "organization": "Demo Warehouse",
        "official_job_title": "Warehouse Associate",
        "statement": "Explained warehouse safety rules to Chinese employees and helped onboard new hires.",
        "skills": ["Safety Communication", "Onboarding"],
        "verification_status": "confirmed",
    },
    {
        "id": "CF-PENDING",
        "organization": "Demo Warehouse",
        "official_job_title": "Warehouse Associate",
        "statement": "Possible unconfirmed fact.",
        "skills": [],
        "verification_status": "needs_confirmation",
    },
]
REQS = [{"id": "R-01"}, {"id": "R-02"}]


def run(edit):
    return validate_and_apply_edits(BASE, [edit], FACTS, REQS)


def test_rewrite_with_confirmed_fact_is_applied():
    result = run({
        "action": "reframe",
        "target_section": "Experience",
        "target_text": "",
        "original_text": "Explained safety rules to Chinese employees.",
        "proposed_text": "Communicated warehouse safety requirements to Chinese-speaking employees.",
        "fact_ids": ["CF-1"],
        "requirement_ids": ["R-01"],
        "modification_reason": "Emphasize safety communication",
    })
    assert len(result.applied) == 1
    assert "Communicated warehouse safety" in result.content


def test_unconfirmed_fact_is_rejected():
    result = run({
        "action": "add_from_confirmed_fact",
        "target_section": "Experience",
        "target_text": "Explained safety rules to Chinese employees.",
        "original_text": "",
        "proposed_text": "Added an unsupported statement.",
        "fact_ids": ["CF-PENDING"],
        "requirement_ids": ["R-01"],
        "modification_reason": "",
    })
    assert not result.applied
    assert "未确认" in result.rejected[0]["reason"]


def test_new_number_is_rejected():
    result = run({
        "action": "rewrite",
        "target_section": "Experience",
        "target_text": "",
        "original_text": "Explained safety rules to Chinese employees.",
        "proposed_text": "Trained 25 employees and improved safety by 40%.",
        "fact_ids": ["CF-1"],
        "requirement_ids": ["R-01"],
        "modification_reason": "",
    })
    assert not result.applied
    assert "数字" in result.rejected[0]["reason"]


def test_identity_line_cannot_be_rewritten():
    result = run({
        "action": "rewrite",
        "target_section": "Experience",
        "target_text": "",
        "original_text": "Warehouse Associate",
        "proposed_text": "Warehouse Supervisor",
        "fact_ids": ["CF-1"],
        "requirement_ids": ["R-02"],
        "modification_reason": "",
    })
    assert not result.applied
    assert "正式职位" in result.rejected[0]["reason"]


def test_add_from_confirmed_fact_keeps_base_and_adds_text():
    result = run({
        "action": "add_from_confirmed_fact",
        "target_section": "Experience",
        "target_text": "Explained safety rules to Chinese employees.",
        "original_text": "",
        "proposed_text": "Helped onboard new hires on warehouse safety procedures.",
        "fact_ids": ["CF-1"],
        "requirement_ids": ["R-02"],
        "modification_reason": "Add confirmed onboarding evidence",
    })
    assert len(result.applied) == 1
    assert "Helped onboard new hires" in result.content

