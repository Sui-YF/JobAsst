from __future__ import annotations

from dataclasses import asdict, dataclass, field


DIMENSIONS = (
    "requirement_coverage", "match_boundary", "evidence_grounding", "truthfulness",
    "positioning_quality", "content_prioritization", "resume_readability",
)


@dataclass
class HumanScorecard:
    fixture_id: str
    fixture_version: int
    reviewer: str
    scores: dict[str, int] = field(default_factory=dict)
    critical_truthfulness_failure: bool = False
    notes: str = ""

    def validate(self) -> None:
        if set(self.scores) != set(DIMENSIONS):
            raise ValueError("Human Scorecard必须填写全部质量维度")
        if any(value not in {0, 1, 2} for value in self.scores.values()):
            raise ValueError("每项分数只能是0/1/2")

    @property
    def passed(self) -> bool:
        self.validate()
        return not self.critical_truthfulness_failure and min(self.scores.values()) >= 1

    def to_dict(self) -> dict:
        self.validate()
        return asdict(self) | {"passed": self.passed}


def scorecard_template(fixture_id: str, fixture_version: int) -> dict:
    return HumanScorecard(
        fixture_id=fixture_id, fixture_version=fixture_version, reviewer="",
        scores={dimension: 0 for dimension in DIMENSIONS},
        notes="只评价模型输出质量；不得写入或修改Career Evidence/User Claim。",
    ).to_dict()
