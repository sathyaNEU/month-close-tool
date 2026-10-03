"""The active configuration journals need: the entity set, templates, the approval matrix, chart and dimensions."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection

from close_app import workflow_configs
from close_app.errors import Problem
from close_contracts.config.journal_workflow import ApprovalMatrix, Entity, EntitySet, JournalTemplate
from close_contracts.config.journaling import ChartOfAccounts, DimensionSet
from close_contracts.dsl import Issue
from close_contracts.journals import JournalDraft, journal_issues


@dataclass(frozen=True)
class Setup:
    entities: EntitySet
    templates: dict[str, tuple[JournalTemplate, int]]  # id -> (template, version)
    matrix: ApprovalMatrix
    matrix_version: int
    chart: ChartOfAccounts
    dimensions: DimensionSet | None

    def entity(self, entity_id: str) -> Entity:
        found = self.entities.entity(entity_id)
        if found is None:
            raise Problem(
                422, "Unknown entity", f"{entity_id} is not in the active entity set", kind="validation-failed"
            )
        return found


def active(tx: Connection) -> Setup:
    """The active configuration, or a 409 naming what isn't active yet."""
    ctx = workflow_configs.context(tx, ("active",))
    missing = [
        name
        for name, value in (("an entity set", ctx.entities), ("an approval matrix", ctx.matrix), ("a chart", ctx.chart))
        if value is None
    ]
    if ctx.entities is None or ctx.matrix is None or ctx.chart is None:
        detail = f"activate {', '.join(missing)} first"
        raise Problem(409, "The journal workflow isn't set up", detail, kind="conflict")
    return Setup(
        entities=ctx.entities,
        templates=dict(ctx.templates),
        matrix=ctx.matrix[0],
        matrix_version=ctx.matrix[1],
        chart=ctx.chart,
        dimensions=ctx.dimensions,
    )


def checked(setup: Setup, draft: JournalDraft) -> tuple[JournalTemplate, int, Entity]:
    """The draft's template, its version and the entity, or a 422 listing every problem."""
    entity = setup.entity(draft.entity_id)
    found = setup.templates.get(draft.template_id)
    if found is None:
        issue = Issue("unknown_template", "/template_id", f"template {draft.template_id} is not active")
        raise invalid([issue])
    template, version = found
    issues = journal_issues(
        draft, template=template, chart=setup.chart, dimensions=setup.dimensions, currency=entity.functional_currency
    )
    if issues:
        raise invalid(issues)
    return template, version, entity


def invalid(issues: list[Issue], title: str = "The journal is invalid") -> Problem:
    listed = [{"code": i.code, "path": i.path, "message": i.message} for i in issues]
    return Problem(422, title, kind="validation-failed", issues=listed)
