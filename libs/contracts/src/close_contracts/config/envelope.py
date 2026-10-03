"""The configuration envelope (roadmap engineering rule 2): what every configuration version carries.

A configuration (a data source, a match set, the accounting policy, and later journal templates and
posting rules) moves through draft -> submitted -> approved -> active, and later retired. The envelope
holds that status, the effective dates, the author and the approver, and an RFC 8785 content hash of
the specification, and it enforces maker-checker: the author can never be the approver. Timestamps and
the approval trail live in the control-plane database (Stage 4); the envelope is what the engine reads.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from close_contracts.canonical_json import content_sha256

SLUG: Final = r"^[a-z][a-z0-9_]{2,63}$"
PRINCIPAL: Final = r"^[A-Za-z0-9._@-]{1,128}$"


class ConfigStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    ACTIVE = "active"
    RETIRED = "retired"


class ConfigKind(StrEnum):
    DATA_SOURCE = "data_source"
    MATCH_SET = "match_set"
    ACCOUNTING_POLICY = "accounting_policy"
    EVENT_TYPE = "event_type"  # Stage 6: journaling configuration (ADR-0015)
    AUTO_JOURNAL_DEFINITION = "auto_journal_definition"
    ACCOUNT_MAPPING = "account_mapping"
    CHART_OF_ACCOUNTS = "chart_of_accounts"
    DIMENSION_SET = "dimension_set"
    ENTITY_SET = "entity_set"  # Stage 7: the journal workflow (ADR-0016)
    JOURNAL_TEMPLATE = "journal_template"
    APPROVAL_MATRIX = "approval_matrix"
    RECURRING_JOURNAL = "recurring_journal"
    CLOSE_CHECKLIST = "close_checklist"  # Stage 8b: the period close (ADR-0020)
    FLUX_RULES = "flux_rules"


_NEEDS_APPROVER: Final = frozenset({ConfigStatus.APPROVED, ConfigStatus.ACTIVE, ConfigStatus.RETIRED})


def spec_hash(spec: BaseModel) -> str:
    """SHA-256 of the specification's canonical JSON (RFC 8785)."""
    data: Any = spec.model_dump(mode="json")
    return content_sha256(data)


class Envelope[SpecT: BaseModel](BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ConfigKind
    config_id: str = Field(pattern=SLUG)
    version: int = Field(ge=1, le=2**31 - 1)
    status: ConfigStatus
    effective_from: date
    effective_to: date | None = None
    author: str = Field(pattern=PRINCIPAL)
    approver: str | None = Field(default=None, pattern=PRINCIPAL)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: SpecT

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        if self.approver is not None and self.approver == self.author:
            raise ValueError("maker-checker: the author cannot approve their own configuration")
        if self.status in _NEEDS_APPROVER and self.approver is None:
            raise ValueError(f"a {self.status.value} configuration needs an approver")
        if self.status not in _NEEDS_APPROVER and self.approver is not None:
            raise ValueError(f"a {self.status.value} configuration cannot have an approver yet")
        if self.content_hash != spec_hash(self.spec):
            raise ValueError("content_hash does not match the specification")
        return self

    @classmethod
    def seal(cls, spec: SpecT, **fields: Any) -> Self:
        """Build an envelope with the content hash computed from ``spec``."""
        return cls(spec=spec, content_hash=spec_hash(spec), **fields)
