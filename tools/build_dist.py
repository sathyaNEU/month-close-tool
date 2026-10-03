"""Build the artifacts the container images install, from ``uv.lock`` only.

    dist/app/    the Databricks App deploy artifact: app.yaml, migrations, our wheels, hashed
                 requirements for everything else, a pyproject and .python-version pinning 3.12,
                 and MANIFEST.json (sha256 of every file). The close-app image installs exactly this.
    dist/spark/  the engine runtime for the local Lakeflow Jobs stand-in: hashed requirements
                 (engine + jobs emulator + the DBR-parity ``local`` group), wheels, the bundle and the
                 spark-submit launcher. The close-spark image installs exactly this.

    uv run python tools/build_dist.py app|spark|all

Wheels are built with a fixed SOURCE_DATE_EPOCH so unchanged code gives byte-identical files (and
cached image layers). The app requirements are checked to contain no Spark, data or test packages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "dist"
EPOCH = "315532800"  # 1980-01-01, the earliest time a zip can hold
# The app talks to Spark only through Spark Connect (pyspark-client), so the full pyspark (with its JVM
# jars), Delta and test tooling must never reach the deploy artifact.
APP_FORBIDDEN = {"pyspark", "delta-spark", "py4j", "pytest", "hypothesis"}
APP_REQUIRED = {"pyspark-client"}
_REQ_START = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<spec>\S*)")


def _uv(*args: str) -> str:
    env = {**os.environ, "SOURCE_DATE_EPOCH": EPOCH}
    proc = subprocess.run(["uv", *args], cwd=REPO, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"uv {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def _wheels(out: Path, *packages: str) -> None:
    for package in packages:
        _uv("build", "--package", package, "--wheel", "--out-dir", str(out), "--quiet")


def requirement_blocks(text: str) -> dict[str, str]:
    """Split ``uv export`` output into one block per requirement (spec line + hash lines), keyed by
    normalised name. Comments are dropped."""
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _REQ_START.match(line)
        if m and not line[0].isspace():
            current = re.sub(r"[-_.]+", "-", m.group("name")).lower()
            if current in blocks:
                raise SystemExit(f"duplicate requirement {current} in one export")
            blocks[current] = [line]
        elif current is not None:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def _pin(block: str) -> tuple[str, str | None, tuple[str, ...]]:
    """A block's requirement (name and version), its environment marker, and its hashes."""
    spec, *rest = block.splitlines()
    requirement, _, marker = spec.removesuffix("\\").strip().partition(" ; ")
    hashes = tuple(ln.strip().removesuffix("\\").strip() for ln in rest if ln.strip().startswith("--hash="))
    return requirement.strip(), marker.strip() or None, hashes


def _broader(a: str, b: str) -> str | None:
    """Two blocks for one package that pin the same version and hashes, differing only in their markers, merge
    into the broader one: an unconditional requirement covers a conditional one (pandas needs tzdata everywhere,
    psycopg only on Windows). Anything else is a real disagreement (None)."""
    (req_a, marker_a, hashes_a), (req_b, marker_b, hashes_b) = _pin(a), _pin(b)
    if (req_a, hashes_a) != (req_b, hashes_b):
        return None
    if marker_a is None:
        return a
    if marker_b is None:
        return b
    return a if marker_a == marker_b else None


def merge(*exports: str) -> str:
    merged: dict[str, str] = {}
    for text in exports:
        for name, block in requirement_blocks(text).items():
            if name not in merged or merged[name] == block:
                merged[name] = block
                continue
            kept = _broader(merged[name], block)
            if kept is None:
                raise SystemExit(f"exports disagree on {name}")
            merged[name] = kept
    return "\n".join(merged[name] for name in sorted(merged)) + "\n"


def _manifest(root: Path) -> None:
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != "MANIFEST.json")
    manifest = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    root.joinpath("MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _fresh(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def build_app() -> Path:
    out = _fresh(DIST / "app")
    _wheels(out / "wheels", "close-contracts", "close-platform", "close-app")
    reqs = _uv(
        "export", "--frozen", "--no-default-groups", "--package", "close-app", "--extra", "connect",
        "--no-emit-workspace", "--no-header", "--format", "requirements-txt",
    )  # fmt: skip
    names = set(requirement_blocks(reqs))
    leaked, missing = APP_FORBIDDEN & names, APP_REQUIRED - names
    if leaked or missing:
        raise SystemExit(f"app requirements: must not contain {sorted(leaked)}, must contain {sorted(missing)}")
    out.joinpath("requirements.txt").write_text(merge(reqs))
    shutil.copy2(REPO / "services" / "app" / "app.yaml", out / "app.yaml")
    shutil.copytree(
        REPO / "services" / "app" / "migrations", out / "migrations", ignore=shutil.ignore_patterns("__pycache__")
    )
    out.joinpath(".python-version").write_text("3.12\n")
    out.joinpath("pyproject.toml").write_text(
        '[project]\nname = "close-app-deploy"\nversion = "0.1.0"\nrequires-python = "==3.12.*"\n'
    )
    _manifest(out)
    return out


def build_spark() -> Path:
    out = _fresh(DIST / "spark")
    _wheels(out / "wheels", "close-contracts", "close-engine", "close-datagen", "close-jobs-emulator")
    local = _uv(
        "export", "--frozen", "--only-group", "local", "--no-emit-workspace", "--no-header",
        "--format", "requirements-txt",
    )  # fmt: skip
    services = _uv(
        "export", "--frozen", "--no-default-groups", "--package", "close-jobs-emulator", "--package", "close-engine",
        "--package", "close-datagen",
        "--no-emit-workspace", "--no-header", "--format", "requirements-txt",
    )  # fmt: skip
    out.joinpath("requirements.txt").write_text(merge(local, services))
    engine = out / "engine"
    (engine / "scripts").mkdir(parents=True)
    shutil.copy2(REPO / "engine" / "databricks.yml", engine / "databricks.yml")
    shutil.copy2(REPO / "engine" / "scripts" / "run_job.py", engine / "scripts" / "run_job.py")
    _manifest(out)
    return out


def main(argv: list[str]) -> int:
    targets = {"app": [build_app], "spark": [build_spark], "all": [build_app, build_spark]}
    if len(argv) != 1 or argv[0] not in targets:
        raise SystemExit("usage: build_dist.py app|spark|all")
    for build in targets[argv[0]]:
        out = build()
        files = sum(1 for p in out.rglob("*") if p.is_file())
        print(f"built {out.relative_to(REPO)}: {files} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
