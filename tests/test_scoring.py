from scoring import calculate_eligibility, calculate_scores, requirement_weight


def test_baseline_weights():
    assert requirement_weight("must_have") == 3
    assert requirement_weight("core_responsibility") == 3
    assert requirement_weight("preferred") == 1
    assert requirement_weight("supporting") == 1
    assert requirement_weight("preferred", explicit_critical=True) == 4


def test_fixed_score_and_coverage():
    requirements = [
        {"nature": "match", "weight": 3, "match_status": "strong_match"},
        {"nature": "match", "weight": 3, "match_status": "partial_match"},
        {"nature": "match", "weight": 1, "match_status": "needs_confirmation"},
        {"nature": "eligibility", "weight": 3, "eligibility_status": "not_met"},
    ]
    result = calculate_scores(requirements)
    assert result.earned_points == 4.95
    assert result.total_weight == 7
    assert result.match_score == 68
    assert result.evidence_coverage == 86


def test_no_evidence_not_met_and_needs_confirmation_score_zero():
    requirements = [
        {"nature": "match", "weight": 1, "match_status": "no_evidence"},
        {"nature": "match", "weight": 1, "match_status": "not_met"},
        {"nature": "match", "weight": 1, "match_status": "needs_confirmation"},
    ]
    result = calculate_scores(requirements)
    assert result.match_score == 13
    assert result.evidence_coverage == 33


def test_v011_fit_score_calibration():
    direct = calculate_scores([{"nature": "match", "weight": 3, "match_status": "direct_strong"}])
    transferable = calculate_scores([{"nature": "match", "weight": 3, "match_status": "transferable_match"}])
    unknown = calculate_scores([{"nature": "match", "weight": 3, "match_status": "no_evidence"}])
    assert direct.match_score == 90
    assert transferable.match_score == 52
    assert unknown.match_score == 10


def test_eligibility_is_separate():
    assert calculate_eligibility([]) == "未发现明确硬性资格项"
    assert calculate_eligibility([{"nature": "eligibility", "eligibility_status": "met"}]) == "符合"
    assert calculate_eligibility([{"nature": "eligibility", "eligibility_status": "needs_confirmation"}]) == "待确认"
    assert "不符合" in calculate_eligibility([{"nature": "eligibility", "eligibility_status": "not_met"}])
