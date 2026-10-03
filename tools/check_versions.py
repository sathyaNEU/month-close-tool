"""Verify the local toolchain and Python environment against tools/versions.lock.

Run through uv (``uv run python tools/check_versions.py``) so the Python package checks see the
project environment. Exits non-zero on any mismatch.
"""

from __future__ import annotations

import importlib.metadata as md
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "tools" / "versions.lock"

Row = tuple[str, str, str, bool]


def _run(cmd: list[str]) -> str:
    exe = shutil.which(cmd[0])
    if exe is None:
        return ""
    proc = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, check=False, timeout=60)
    return (proc.stdout + proc.stderr).strip()


def _grab(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _java_home() -> Path | None:
    env = os.environ.get("JAVA_HOME")
    if env:
        return Path(env)
    brew_jdk = Path("/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home")
    return brew_jdk if brew_jdk.exists() else None


def _exact(name: str, want: str, got: str | None) -> Row:
    return (name, want, got or "missing", got == want)


def _at_least(name: str, minimum: str, got: str | None) -> Row:
    ok = got is not None and _version_tuple(got) >= _version_tuple(minimum)
    return (name, f">= {minimum}", got or "missing", ok)


def _bundled_hadoop() -> str | None:
    try:
        import pyspark
    except ImportError:
        return None
    for jar in (Path(pyspark.__file__).parent / "jars").glob("hadoop-client-api-*.jar"):
        found = _grab(r"api-(\d+\.\d+\.\d+)\.jar$", jar.name)
        if found:
            return found
    return None


def image_references() -> list[tuple[str, str]]:
    """(file, image) for every image the compose files and Dockerfiles use, except project images."""
    refs = []
    for compose in sorted(REPO.glob("compose*.yaml")):
        text = compose.read_text(encoding="utf-8")
        refs += [(compose.name, m.group(1)) for m in re.finditer(r"^\s*image:\s*(\S+)", text, re.MULTILINE)]
    makefile = REPO.joinpath("Makefile").read_text(encoding="utf-8")
    refs += [("Makefile", m.group(1)) for m in re.finditer(r"^[A-Z_]+_IMAGE\s*:=\s*(\S+)", makefile, re.MULTILINE)]
    for dockerfile in sorted(REPO.joinpath("deploy", "docker").glob("*/Dockerfile")):
        text = dockerfile.read_text(encoding="utf-8")
        where = str(dockerfile.relative_to(REPO))
        refs += [(where, m.group(1)) for m in re.finditer(r"^ARG \w+_IMAGE=(\S+)", text, re.MULTILINE)]
    return [(where, ref) for where, ref in refs if not ref.endswith(":local")]


def collect(lock: dict[str, dict[str, object]]) -> list[Row]:
    tc = lock["toolchain"]
    rows: list[Row] = [
        _at_least("uv", str(tc["uv_min"]), _grab(r"uv (\d+\.\d+\.\d+)", _run(["uv", "--version"]))),
    ]
    java_home = _java_home()
    java_out = _run([str(java_home / "bin" / "java"), "-version"]) if java_home else ""
    rows.append(_exact("java (JAVA_HOME)", str(tc["java_major"]), _grab(r'version "(\d+)', java_out)))
    docker = _grab(r"^(\d+\.\d+\.\d+)$", _run(["docker", "version", "--format", "{{.Server.Version}}"]))
    rows.append(_at_least("docker server", str(tc["docker_server_min"]), docker))
    rows.append(
        (
            "python (uv env)",
            str(tc["python"]),
            sys.version.split()[0],
            f"{sys.version_info.major}.{sys.version_info.minor}" == tc["python"],
        )
    )
    for pkg, want in lock["python_packages"].items():
        try:
            got: str | None = md.version(pkg)
        except md.PackageNotFoundError:
            got = None
        rows.append(_exact(f"py:{pkg}", str(want), got))
    hadoop_aws = str(lock["jars"]["hadoop_aws"]).rsplit(":", 1)[1]
    rows.append(_exact("hadoop-aws == bundled hadoop", hadoop_aws, _bundled_hadoop()))
    pinned = {str(v) for v in lock["images"].values()}
    for where, ref in sorted(set(image_references())):
        rows.append((f"image in {where}", "pinned", ref, ref in pinned))
    return rows


def main() -> int:
    rows = collect(tomllib.loads(LOCK.read_text(encoding="utf-8")))
    width = max(len(r[0]) for r in rows)
    for name, want, got, ok in rows:
        print(f"{'OK ' if ok else 'BAD'}  {name:<{width}}  want {want:<12}  got {got}")
    bad = [r for r in rows if not r[3]]
    print(f"check-versions: {len(rows) - len(bad)}/{len(rows)} OK")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
