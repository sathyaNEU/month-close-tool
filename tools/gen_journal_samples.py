"""Write or check the SAMPLE journaling configuration as the JSON bodies the API accepts.

    uv run python tools/gen_journal_samples.py           # rewrite docs/samples/journaling/
    uv run python tools/gen_journal_samples.py --check   # exit 1 when the files differ from the samples

Each file holds one request body (``effective_from`` and ``spec``) for one resource, numbered in the order the
configurations must be activated: chart, dimensions, policy, mappings, event types and definitions (Stage 6),
then entities, the approval matrix, templates and recurring journals (Stage 7), then the close checklist and the
flux rules (Stage 8b).
These are samples for testing, not anyone's accounting (``docs/domain/sample-journal-rules.md``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from close_contracts.samples import close
from close_contracts.samples import journal_workflow as workflow
from close_contracts.samples import journaling as sample

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "samples" / "journaling"
NOTE = "SAMPLE configuration for testing, not your company's accounting (docs/domain/sample-journal-rules.md)"
EFFECTIVE_FROM = "2026-06-01"


def bodies() -> dict[str, dict[str, Any]]:
    items: list[tuple[str, str, Any]] = [
        ("accounts", sample.SAMPLE_CHART.chart_id, sample.SAMPLE_CHART),
        ("dimensions", sample.SAMPLE_DIMENSIONS.dimension_set_id, sample.SAMPLE_DIMENSIONS),
        ("programs", "sample_accounting_policy", sample.SAMPLE_POLICY),
        *(("account-mappings", m.mapping_id, m) for m in sample.SAMPLE_MAPPINGS),
        *(("event-types", e.type_id, e) for e in sample.SAMPLE_EVENT_TYPES),
        *(("auto-journal-definitions", d.definition_id, d) for d in sample.SAMPLE_DEFINITIONS),
        ("entities", workflow.SAMPLE_ENTITIES.entity_set_id, workflow.SAMPLE_ENTITIES),
        ("approval-matrices", workflow.SAMPLE_APPROVAL_MATRIX.matrix_id, workflow.SAMPLE_APPROVAL_MATRIX),
        *(("journal-templates", t.template_id, t) for t in workflow.SAMPLE_TEMPLATES),
        *(("recurring-journals", r.recurring_id, r) for r in workflow.SAMPLE_RECURRING),
        ("close-checklists", close.SAMPLE_CHECKLIST.checklist_id, close.SAMPLE_CHECKLIST),  # Stage 8b
        ("flux-rules", close.SAMPLE_FLUX_RULES.rules_id, close.SAMPLE_FLUX_RULES),
    ]
    out = {}
    for n, (resource, config_id, model) in enumerate(items, start=1):
        out[f"{n:02d}-{resource}-{config_id}.json"] = {
            "_sample": NOTE,
            "resource": resource,
            "body": {"effective_from": EFFECTIVE_FROM, "spec": model.model_dump(mode="json")},
        }
    return out


def render(body: dict[str, Any]) -> str:
    return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    current = {name: render(body) for name, body in bodies().items()}
    if args.check:
        on_disk = {p.name: p.read_text(encoding="utf-8") for p in OUT.glob("*.json")} if OUT.exists() else {}
        drift = sorted(set(current) ^ set(on_disk) | {n for n in current if on_disk.get(n) != current[n]})
        sys.stdout.write(f"journaling samples: {len(current)} files, {len(drift)} drift\n")
        return 1 if drift else 0
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.json"):
        if stale.name not in current:
            stale.unlink()
    for name, text in current.items():
        (OUT / name).write_text(text, encoding="utf-8")
    sys.stdout.write(f"wrote {len(current)} journaling samples to {OUT.relative_to(REPO)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
