"""Supporting documents on a journal, stored in Postgres (the app holds no S3 keys, a Stage 1 rule).

- **Uploads** are base64 in JSON, so the app needs no multipart parser. They are capped at 10 MiB, limited to an
  allow-list of content types, and hashed with SHA-256.
- **Drafts only.** Documents are added and removed while the journal is a draft, before anyone approves it. A
  removed document is kept, marked removed.
- **Downloads** always carry ``Content-Disposition: attachment`` and ``X-Content-Type-Options: nosniff``, so an
  uploaded page or image can't run script in the app's origin.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from close_app.errors import Problem
from close_app.identity import Principal
from close_app.journals import store, workflow

MAX_BYTES: Final = 10 * 1024 * 1024
ALLOWED: Final = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "text/csv",
        "text/plain",
        "message/rfc822",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
_SAFE_NAME: Final = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,255}$")
_INSERT: Final = text(
    "INSERT INTO attachments (attachment_id, journal_id, file_name, content_type, size_bytes, sha256, content, "
    "uploaded_by) VALUES (:id, :j, :name, :type, :size, :sha, :content, :by)"
)
_LIST: Final = text(
    "SELECT attachment_id, file_name, content_type, size_bytes, sha256, uploaded_by, uploaded_at, removed_by, "
    "removed_at FROM attachments WHERE journal_id = :j ORDER BY uploaded_at, attachment_id"
)
_ONE: Final = text("SELECT * FROM attachments WHERE attachment_id = :id AND journal_id = :j")
_REMOVE: Final = text(
    "UPDATE attachments SET removed_by = :by, removed_at = clock_timestamp() WHERE attachment_id = :id"
)


def _meta(row: RowMapping) -> dict[str, Any]:
    out = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items() if k != "content"}
    out["attachment_id"] = str(row["attachment_id"])
    out.pop("journal_id", None)
    return out


def add(
    tx: Connection,
    journal: RowMapping,
    *,
    p: Principal,
    file_name: str,
    content_type: str,
    content_base64: str,
    request_id: str | None,
) -> dict[str, Any]:
    workflow.need_status(journal, "draft", verb="attach a document to")
    workflow.preparer_only(p, journal, "attach documents to it")
    if not _SAFE_NAME.fullmatch(file_name):
        raise Problem(422, "The file name isn't allowed", "1-255 characters, no slashes or control characters")
    if content_type not in ALLOWED:
        raise Problem(415, "The content type isn't allowed", f"allowed: {', '.join(sorted(ALLOWED))}")
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise Problem(422, "The content isn't valid base64", kind="validation-failed") from None
    if not 1 <= len(content) <= MAX_BYTES:
        raise Problem(413, "The document is empty or too large", f"1 byte to {MAX_BYTES} bytes")
    digest = hashlib.sha256(content).hexdigest()
    attachment_id = uuid.uuid4()
    tx.execute(
        _INSERT,
        {
            "id": attachment_id,
            "j": journal["journal_id"],
            "name": file_name,
            "type": content_type,
            "size": len(content),
            "sha": digest,
            "content": content,
            "by": p.user_id,
        },
    )
    store.audit_event(
        tx, p.user_id, "attach", journal, request_id, attachment_id=str(attachment_id), file_name=file_name,
        size_bytes=len(content), sha256=digest,
    )  # fmt: skip
    row = tx.execute(_ONE, {"id": attachment_id, "j": journal["journal_id"]}).mappings().one()
    return _meta(row)


def remove(
    tx: Connection, journal: RowMapping, attachment_id: UUID, *, p: Principal, request_id: str | None
) -> dict[str, Any]:
    workflow.need_status(journal, "draft", verb="remove a document from")
    workflow.preparer_only(p, journal, "remove documents from it")
    row = tx.execute(_ONE, {"id": attachment_id, "j": journal["journal_id"]}).mappings().first()
    if row is None:
        raise Problem(404, "Not found", f"attachment {attachment_id}", kind="not-found")
    if row["removed_at"] is not None:
        raise Problem(409, "The document is already removed", kind="conflict")
    tx.execute(_REMOVE, {"id": attachment_id, "by": p.user_id})
    store.audit_event(tx, p.user_id, "detach", journal, request_id, attachment_id=str(attachment_id))
    return _meta(tx.execute(_ONE, {"id": attachment_id, "j": journal["journal_id"]}).mappings().one())


def listing(conn: Connection, journal: RowMapping) -> list[dict[str, Any]]:
    return [_meta(r) for r in conn.execute(_LIST, {"j": journal["journal_id"]}).mappings()]


def content(conn: Connection, journal: RowMapping, attachment_id: UUID) -> RowMapping | None:
    return conn.execute(_ONE, {"id": attachment_id, "j": journal["journal_id"]}).mappings().first()
