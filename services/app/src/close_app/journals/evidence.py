"""What auditors ask for (AS 2401, PCAOB Audit Focus on journal entries).

- **The journal population** lists every journal: its number, status and source, the preparer, the approvers of its
  latest submission (with anyone they stood in for), its timestamps, risk factors, whether it was submitted or
  approved after its period ended, and any bypass of approval. In Stage 7a it covers the control plane's journals;
  the engine's automated entries join it with posting in 7b.
- **Foreign currencies (Stage 7c).** Each journal shows its currency and its functional currency and totals, and a
  converted one its rate (with the rate's date, type, version and source) and its rounding line's amount.
- **The completeness summary** ties it to the journal table: per entity the count, the first and last numbers,
  whether the numbers are gapless, and whether the counter agrees.
- **Spreadsheet safety.** Auditors open the export in Excel, so typed text that starts like a formula (``=``, ``+``,
  ``-``, ``@``) is prefixed with a quote and shows as text.
- **The exception report** lists journals rejected, recalled or cancelled, bypassed approval, or whose automatic
  reversal failed; rejected imports; and recurring journals that couldn't be created.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import Connection, text

POPULATION_COLUMNS: Final = (
    "entity_id", "number", "journal_id", "status", "source", "period", "accounting_date", "template_id",
    "template_version", "currency", "total_debit_minor", "line_count", "description", "created_by", "created_at",
    "submitted_by", "submitted_at", "approvals", "approved_at", "bypass_reason", "risk_score", "risk_factors",
    "post_close", "reverses_number", "batch_id", "import_id", "recurring_id", "recurring_period", "posting_run_id",
    "posted_at", "erp_document_id", "erp_confirmed_at", "functional_currency", "functional_debit_minor",
    "ledger_line_count", "fx_rate", "fx_rate_date", "fx_rate_type", "fx_rate_version", "fx_rate_source",
    "fx_rounding_minor", "fx_rate_reused_from",
)  # fmt: skip
_POPULATION: Final = text(
    "SELECT j.*, "
    "(SELECT string_agg(a.level || ':' || a.approver || COALESCE(' for ' || a.on_behalf_of, ''), '; ' "
    "  ORDER BY a.level) FROM journal_approvals a "
    "  WHERE a.journal_id = j.journal_id AND a.submission = j.submission) AS approvals, "
    "(SELECT string_agg(f.key, ';' ORDER BY f.key) FROM jsonb_each(COALESCE(j.risk_factors, '{}'::jsonb)) f "
    "  WHERE (f.value ->> 'hit')::boolean) AS hit_factors, "
    "(p.ends_on IS NOT NULL AND (j.submitted_at::date > p.ends_on OR j.approved_at::date > p.ends_on)) "
    "  AS post_close, "
    "r.number AS reverses_number, "
    "c.rate AS fx_rate, c.rate_date AS fx_rate_date, c.rate_type AS fx_rate_type, c.rate_version AS fx_rate_version, "
    "c.rate_source AS fx_rate_source, c.rounding_minor AS fx_rounding_minor, "
    "(SELECT o.number FROM journals o WHERE o.journal_id = c.reused_from) AS fx_rate_reused_from "
    "FROM journals j "
    "LEFT JOIN accounting_periods p ON p.entity_id = j.entity_id AND p.period = j.period "
    "LEFT JOIN journals r ON r.journal_id = j.reverses_journal_id "
    "LEFT JOIN journal_conversions c ON c.journal_id = j.journal_id AND c.submission = j.submission "
    "AND j.conversion_hash IS NOT NULL "
    "WHERE j.entity_id = ANY (:e) ORDER BY j.entity_id, j.number"
)
_SUMMARY: Final = text(
    "SELECT j.entity_id, count(*) AS journals, min(j.number) AS first, max(j.number) AS last, "
    "max(c.last_number) AS counter FROM journals j JOIN journal_counters c ON c.entity_id = j.entity_id "
    "WHERE j.entity_id = ANY (:e) GROUP BY j.entity_id ORDER BY j.entity_id"
)
_BY_STATUS: Final = text(
    "SELECT entity_id, status, count(*) AS n FROM journals WHERE entity_id = ANY (:e) GROUP BY entity_id, status"
)
_EXCEPTIONS: Final = text(
    "SELECT 'journal' AS subject, h.action AS kind, h.at, h.actor, h.on_behalf_of, h.reason, h.level, "
    "j.entity_id, j.number, j.journal_id::text AS ref "
    "FROM journal_history h JOIN journals j ON j.journal_id = h.journal_id "
    "WHERE h.action IN ('reject', 'recall', 'cancel', 'bypass', 'auto_reverse_failed', 'post_failed') "
    "AND j.entity_id = ANY (:e) "
    "UNION ALL "
    "SELECT 'import', 'import_rejected', i.imported_at, i.imported_by, NULL, i.issues::text, NULL, i.entity_id, "
    "NULL, i.import_id::text FROM journal_imports i WHERE i.status = 'rejected' AND i.entity_id = ANY (:e) "
    "UNION ALL "
    "SELECT 'recurring', n.kind, n.created_at, 'system:journal-workflow', NULL, n.message, "
    "NULL, n.entity_id, NULL, n.dedup_key FROM notifications n "
    "WHERE n.kind = 'recurring_skipped' AND n.entity_id = ANY (:e) "
    "ORDER BY at, kind"
)


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


_FORMULA_START: Final = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> Any:
    """A CSV cell a spreadsheet won't run: text a preparer typed that starts like a formula gets a leading quote."""
    plain = _plain(value)
    if isinstance(plain, str) and plain.startswith(_FORMULA_START):
        return "'" + plain
    return plain


def population_csv(conn: Connection, entity_ids: list[str]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(POPULATION_COLUMNS)
    for row in conn.execute(_POPULATION, {"e": entity_ids}).mappings():
        values = dict(row) | {"risk_factors": row["hit_factors"] or "", "approvals": row["approvals"] or ""}
        writer.writerow(["" if values[c] is None else _cell(values[c]) for c in POPULATION_COLUMNS])
    return out.getvalue()


def summary(conn: Connection, entity_ids: list[str]) -> list[dict[str, Any]]:
    statuses: dict[str, dict[str, int]] = {}
    for entity_id, status, n in conn.execute(_BY_STATUS, {"e": entity_ids}):
        statuses.setdefault(str(entity_id), {})[str(status)] = int(n)
    out = []
    for row in conn.execute(_SUMMARY, {"e": entity_ids}).mappings():
        count, first, last = int(row["journals"]), int(row["first"]), int(row["last"])
        out.append(
            {
                "entity_id": row["entity_id"],
                "journals": count,
                "first_number": first,
                "last_number": last,
                "gapless": first == 1 and last == count,
                "counter_agrees": int(row["counter"]) == last,
                "by_status": statuses.get(row["entity_id"], {}),
            }
        )
    return out


def exceptions(conn: Connection, entity_ids: list[str]) -> list[dict[str, Any]]:
    return [{k: _plain(v) for k, v in row.items()} for row in conn.execute(_EXCEPTIONS, {"e": entity_ids}).mappings()]
