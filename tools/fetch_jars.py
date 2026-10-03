"""Download the jars pinned in tools/jars.lock into a directory, verifying every SHA-256.

Used by the close-spark image build (stdlib only, so it runs before anything else is installed).
A file that already exists with the right hash is kept; anything else is downloaded again, and a
hash mismatch fails the build.

    python tools/fetch_jars.py /opt/close/jars
"""

from __future__ import annotations

import hashlib
import sys
import tomllib
import urllib.request
from pathlib import Path

LOCK = Path(__file__).resolve().parent / "jars.lock"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(dest: Path, lock: Path = LOCK) -> int:
    jars = tomllib.loads(lock.read_text(encoding="utf-8"))["jar"]
    dest.mkdir(parents=True, exist_ok=True)
    for jar in jars:
        target = dest.joinpath(jar["file"])
        if target.exists() and _sha256(target) == jar["sha256"]:
            continue
        if not jar["url"].startswith("https://repo1.maven.org/maven2/"):
            raise SystemExit(f"refusing non-Maven-Central URL: {jar['url']}")
        partial = target.with_suffix(".part")
        with urllib.request.urlopen(jar["url"], timeout=120) as resp, partial.open("wb") as out:  # noqa: S310  # nosec B310
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        actual = _sha256(partial)
        if actual != jar["sha256"]:
            partial.unlink()
            raise SystemExit(f"sha256 mismatch for {jar['coordinate']}: expected {jar['sha256']}, got {actual}")
        partial.replace(target)
        print(f"fetched {jar['coordinate']}")
    extra = {p.name for p in dest.glob("*.jar")} - {j["file"] for j in jars}
    if extra:
        raise SystemExit(f"unexpected jars in {dest}: {sorted(extra)}")
    print(f"{len(jars)} jars verified in {dest}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: fetch_jars.py <dest-dir>")
    sys.exit(fetch(Path(sys.argv[1])))
