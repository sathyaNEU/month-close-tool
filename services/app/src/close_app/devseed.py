"""Seed local development data (role assignments for the devproxy users). Refuses to run outside local.

Users (ids match deploy/devproxy/Caddyfile). Each user's visible entities are the same as in Stage 1:

  alice 1001  sees US01        match_preparer US01; config_author US01 and GLOBAL
  bob   1002  sees EU01        match_preparer EU01; auditor EU01
  carol 1003  sees nothing     no assignments
  dave  1004  sees US01, EU01  match_preparer US01 and EU01; config_approver US01 and GLOBAL;
                               platform_admin and auditor GLOBAL
  erin  1005  sees US01        journal_approver_l2 US01 (Stage 7)
  erp   1006  sees US01        integration_client US01: the stub ERP's machine identity (Stage 7b)
"""

from __future__ import annotations

import os
import sys
import uuid

from sqlalchemy import text

from close_platform.db.credentials import StaticCredentialProvider
from close_platform.db.engine import make_engine

ASSIGNMENTS = [
    ("1001", "match_preparer", "US01"),
    ("1001", "config_author", "US01"),
    ("1001", "config_author", "GLOBAL"),
    ("1002", "match_preparer", "EU01"),
    ("1002", "auditor", "EU01"),
    ("1004", "match_preparer", "US01"),
    ("1004", "match_preparer", "EU01"),
    ("1004", "config_approver", "US01"),
    ("1004", "config_approver", "GLOBAL"),
    ("1004", "platform_admin", "GLOBAL"),
    ("1004", "auditor", "GLOBAL"),
    # Stage 7: alice prepares US01 journals, dave approves level 1 and manages US01 periods, erin approves level 2.
    ("1001", "journal_preparer", "US01"),
    ("1004", "journal_approver_l1", "US01"),
    ("1004", "controller", "US01"),
    ("1005", "journal_approver_l2", "US01"),
    # Stage 7b: the stub ERP reads the ledger feed and writes document numbers back.
    ("1006", "integration_client", "US01"),
]


def main() -> int:
    if os.environ.get("CLOSE_ENV", "local") != "local":
        sys.stderr.write("devseed only runs with CLOSE_ENV=local\n")
        return 2
    engine = make_engine(StaticCredentialProvider.from_env(), pool_size=1, max_overflow=0)
    with engine.begin() as tx:
        for user, role, scope in ASSIGNMENTS:
            tx.execute(
                text(
                    "INSERT INTO role_assignments (assignment_id, user_id, role, scope, granted_by) "
                    "VALUES (:id, :u, :r, :s, 'devseed') ON CONFLICT DO NOTHING"
                ),
                {"id": uuid.uuid4(), "u": user, "r": role, "s": scope},
            )
    sys.stdout.write(f"devseed: {len(ASSIGNMENTS)} role assignments ensured\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
