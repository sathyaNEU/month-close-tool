"""Manual journals and their workflow (Stage 7a, ADR-0016).

- ``store``: rows, gapless numbers, line revisions, the journal history, audit events and JSON.
- ``setup``: the active configuration journals need (entities, templates, the approval matrix, chart, dimensions).
- ``workflow``: create, edit, submit, approve, reject, recall, cancel and reverse, with separation of duties,
  delegation, routing frozen at submit, and automatic reversals.
- ``periods``, ``batches``, ``delegations``, ``attachments``, ``imports``, ``evidence`` and ``tasks`` (recurring
  journals and reminders) build on them.

Every journal is in exactly one entity, in that entity's functional currency (Stage 7a). Rows are never deleted.
"""
