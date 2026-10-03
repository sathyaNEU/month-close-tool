"""Write or check the JSON Schema snapshots of the contract models (roadmap engineering rule 1).

The snapshots under ``libs/contracts/src/close_contracts/schemas/`` are the frozen, versioned form of
each configuration model and of the DSL expression, plus:
- the table registry and the default data sources (the Stage 2b contracts);
- the template match sets (Stage 3);
- the journaling models (Stage 6);
- the close checklist and flux rules (Stage 8b).

A change that alters any of them shows up as drift, which fails ``--check`` (a gate step) until the snapshot
is regenerated on purpose.

    uv run python tools/gen_schemas.py           # rewrite the snapshots
    uv run python tools/gen_schemas.py --check   # exit 1 on any drift
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from close_contracts.config.accounting_policy import AccountingPolicy
from close_contracts.config.close import CloseChecklist, FluxRules
from close_contracts.config.data_source import DataSource
from close_contracts.config.envelope import Envelope
from close_contracts.config.journal_workflow import ApprovalMatrix, EntitySet, JournalTemplate, RecurringJournal
from close_contracts.config.journaling import (
    AccountMapping,
    AutoJournalDefinition,
    ChartOfAccounts,
    DimensionSet,
    EventType,
)
from close_contracts.config.match_set import MatchSet
from close_contracts.default_sources import DEFAULT_SOURCES, IDENTITY_KEYS
from close_contracts.dsl.ast import Expr
from close_contracts.journals import JournalDraft
from close_contracts.table_schemas import ALL_TABLES
from close_contracts.templates import MATCH_SETS

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "libs" / "contracts" / "src" / "close_contracts" / "schemas"


def schemas() -> dict[str, dict[str, Any]]:
    models: dict[str, type[BaseModel]] = {
        "data_source": DataSource,
        "match_set": MatchSet,
        "accounting_policy": AccountingPolicy,
        "envelope_data_source": Envelope[DataSource],
        "envelope_match_set": Envelope[MatchSet],
        "envelope_accounting_policy": Envelope[AccountingPolicy],
        "event_type": EventType,
        "auto_journal_definition": AutoJournalDefinition,
        "account_mapping": AccountMapping,
        "chart_of_accounts": ChartOfAccounts,
        "dimension_set": DimensionSet,
        "entity_set": EntitySet,
        "journal_template": JournalTemplate,
        "approval_matrix": ApprovalMatrix,
        "recurring_journal": RecurringJournal,
        "journal_draft": JournalDraft,
        "close_checklist": CloseChecklist,  # Stage 8b
        "flux_rules": FluxRules,
    }
    out = {name: model.model_json_schema(mode="validation") for name, model in models.items()}
    out["dsl_expression"] = TypeAdapter(Expr).json_schema(mode="validation")
    out["table_registry"] = {
        "tables": {
            s.table.key: {
                "columns": [[c.name, c.type, c.nullable] for c in s.columns],
                "partitioned_by": list(s.partitioned_by),
                "append_only": s.append_only,
                "natural_key": list(s.natural_key),
            }
            for s in ALL_TABLES
        }
    }
    out["default_data_sources"] = {
        "identity_keys": {k: list(v) for k, v in IDENTITY_KEYS.items()},
        "sources": [s.model_dump(mode="json") for s in DEFAULT_SOURCES],
    }
    out["template_match_sets"] = {"match_sets": [m.model_dump(mode="json") for m in MATCH_SETS]}
    return out


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="compare with the snapshots instead of writing")
    args = parser.parse_args(argv)
    current = {f"{name}.schema.json": render(schema) for name, schema in schemas().items()}
    if args.check:
        on_disk = {p.name: p.read_text(encoding="utf-8") for p in OUT.glob("*.schema.json")}
        drift = sorted(n for n in current.keys() | on_disk.keys() if current.get(n) != on_disk.get(n))
        if drift:
            print(f"schema drift in {len(drift)} snapshot(s): {', '.join(drift)} (run tools/gen_schemas.py)")
            return 1
        print(f"schemas: {len(current)} snapshots, 0 drift")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.schema.json"):
        if stale.name not in current:
            stale.unlink()
    for name, text in current.items():
        (OUT / name).write_text(text, encoding="utf-8")
    print(f"wrote {len(current)} snapshots to {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
