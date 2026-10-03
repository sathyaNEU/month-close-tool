"""Run pip-audit on the project environment, honouring reviewed, unexpired waivers only.

Waivers live in tools/security-waivers.toml. An expired waiver fails the run, so an exception can
never outlive its review date silently.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WAIVERS = REPO / "tools" / "security-waivers.toml"


def active_waivers(today: date) -> tuple[list[str], list[str]]:
    data = tomllib.loads(WAIVERS.read_text(encoding="utf-8"))
    active: list[str] = []
    expired: list[str] = []
    for waiver in data.get("pip_audit", []):
        expires = date.fromisoformat(waiver["expires"])
        (active if today <= expires else expired).append(f"{waiver['id']} ({waiver['package']}, expires {expires})")
    return active, expired


def main() -> int:
    today = datetime.now(tz=UTC).date()
    active, expired = active_waivers(today)
    if expired:
        print(f"pip-audit: expired waivers need re-review: {', '.join(expired)}", file=sys.stderr)
        return 1
    ids = [w.split(" ", 1)[0] for w in active]
    cmd = [sys.executable, "-m", "pip_audit", "--skip-editable", "--progress-spinner", "off"]
    for vuln_id in ids:
        cmd += ["--ignore-vuln", vuln_id]
    print(f"pip-audit: applying {len(ids)} reviewed waiver(s): {', '.join(active) or 'none'}")
    return subprocess.run(cmd, check=False, cwd=REPO).returncode


if __name__ == "__main__":
    raise SystemExit(main())
