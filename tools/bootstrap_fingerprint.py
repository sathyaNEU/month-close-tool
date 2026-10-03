"""Print a fingerprint of everything `make bootstrap` can change, to prove a rerun is a no-op.

Gate 0 runs bootstrap, takes a fingerprint, runs bootstrap again, and asserts the two fingerprints
are identical.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"


def _cmd(args: list[str]) -> str:
    exe = shutil.which(args[0])
    if exe is None:
        return "missing"
    return subprocess.run([exe, *args[1:]], capture_output=True, text=True, check=False, cwd=REPO).stdout.strip()


def fingerprint() -> dict[str, object]:
    formulae = [
        line.split('"')[1]
        for line in (REPO / "Brewfile").read_text(encoding="utf-8").splitlines()
        if line.startswith("brew ")
    ]
    brew_versions = _cmd(["brew", "list", "--versions", *formulae])
    packages = _cmd(["uv", "pip", "freeze", "--python", str(REPO / ".venv" / "bin" / "python")])
    secrets_dir = REPO / ".local" / "secrets"
    return {
        "brew": sorted(brew_versions.splitlines()),
        "uv_lock_sha256": _sha(REPO / "uv.lock"),
        "venv_packages_sha256": hashlib.sha256(packages.encode()).hexdigest(),
        "secrets": {p.name: _sha(p) for p in sorted(secrets_dir.glob("*"))} if secrets_dir.exists() else {},
        "pre_commit_hook_sha256": _sha(REPO / ".git" / "hooks" / "pre-commit"),
    }


def main() -> int:
    json.dump(fingerprint(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
