"""Evidence and gap analysis findings used to prioritize the interview."""

from __future__ import annotations

from collections import Counter
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from app.models.interview import Category


class IssueTag(str, Enum):
    STRENGTH = "STRENGTH"
    QUESTION = "QUESTION"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    INCONSISTENCY = "INCONSISTENCY"
    RED_FLAG = "RED_FLAG"


#: How strongly each finding type pulls interview time toward its topic.
TAG_WEIGHT: dict[IssueTag, float] = {
    IssueTag.RED_FLAG: 3.0,
    IssueTag.INCONSISTENCY: 2.5,
    IssueTag.EVIDENCE_GAP: 2.0,
    IssueTag.QUESTION: 1.0,
    IssueTag.STRENGTH: 0.4,  # strengths are still verified, briefly
}


class Issue(BaseModel):
    issue_id: str
    tag: IssueTag
    category: Category
    title: str
    detail: str = ""
    evidence: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    severity: int = Field(2, ge=1, le=3)
    source: Literal["rules", "llm"] = "rules"

    @property
    def priority_weight(self) -> float:
        return TAG_WEIGHT[self.tag] * self.severity / 2

    @property
    def is_concern(self) -> bool:
        return self.tag is not IssueTag.STRENGTH


class GapAnalysis(BaseModel):
    issues: list[Issue] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    llm_used: bool = False

    def for_category(self, category: Category, *, include_resolved: bool = True,
                     resolved: list[str] | None = None) -> list[Issue]:
        resolved_ids = set(resolved or [])
        return [i for i in self.issues if i.category == category
                and (include_resolved or i.issue_id not in resolved_ids)]

    def concerns(self) -> list[Issue]:
        order = [IssueTag.RED_FLAG, IssueTag.INCONSISTENCY, IssueTag.EVIDENCE_GAP, IssueTag.QUESTION]
        return sorted((i for i in self.issues if i.is_concern),
                      key=lambda i: (order.index(i.tag), -i.severity))

    def strengths(self) -> list[Issue]:
        return [i for i in self.issues if i.tag is IssueTag.STRENGTH]

    def counts(self) -> dict[str, int]:
        counter = Counter(i.tag.value for i in self.issues)
        return {tag.value: counter.get(tag.value, 0) for tag in IssueTag}
