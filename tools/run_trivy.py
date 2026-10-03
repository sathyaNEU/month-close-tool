"""Scan our images and container configuration with trivy and write CycloneDX SBOMs (Gate 1b).

Policy:

* ``close-app``, the deployable Databricks App artifact, is gated: a fixable HIGH or CRITICAL finding
  fails the run unless an unexpired ``[[trivy]]`` waiver in ``tools/security-waivers.toml`` covers it.
* ``close-spark``, the local-only stand-in for the Databricks runtime (never deployed), is gated the
  same way for everything we add to it: our pinned jars (``/opt/close/jars``), our wheels and the
  Python packages we install. Findings inside the Apache Spark distribution's own jars
  (``site-packages/pyspark/jars``) are reported, not gated. They change only with the pinned Spark
  version, which mirrors Databricks Runtime 18, and production runs Databricks' own patched runtime.
* ``trivy config`` over our Dockerfiles is gated on HIGH and CRITICAL misconfigurations.
* Third-party images (postgres, seaweedfs, caddy) are reported, not gated.

An expired waiver fails the run, like pip-audit's. SBOMs go to ``dist/sbom/<image>.cdx.json``.

    uv run python tools/run_trivy.py <trivy-image>        (make scan passes the pinned image)
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
WAIVERS = REPO / "tools" / "security-waivers.toml"
SBOM_DIR = REPO / "dist" / "sbom"
CACHE_VOLUME = "close-trivy-cache"
GATED_IMAGES = ("close-app:local", "close-spark:local")
UPSTREAM_SPARK = "/pyspark/jars/"
THIRD_PARTY = ("postgres", "seaweedfs", "caddy")


def _trivy(image: str, *args: str, mount_repo: bool = False, out: Path | None = None) -> str:
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{CACHE_VOLUME}:/root/.cache/trivy",
    ]
    if mount_repo:
        cmd += ["-v", f"{REPO}:/repo:ro"]
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        cmd += ["-v", f"{out}:/out"]
    proc = subprocess.run([*cmd, image, *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"trivy {' '.join(args)} failed:\n{proc.stderr[-3000:]}")
    return proc.stdout


def load_waivers(today: date) -> tuple[list[dict[str, Any]], list[str]]:
    data = tomllib.loads(WAIVERS.read_text(encoding="utf-8"))
    active, expired = [], []
    for waiver in data.get("trivy", []):
        if date.fromisoformat(waiver["expires"]) >= today:
            active.append(waiver)
        else:
            expired.append(
                f"{waiver['id']} ({waiver.get('path') or waiver.get('package')}, expired {waiver['expires']})"
            )
    return active, expired


def waived(finding: dict[str, str], image: str, waivers: list[dict[str, Any]]) -> dict[str, Any] | None:
    for w in waivers:
        if image not in w["images"]:
            continue
        if w["id"] not in {"*", finding["id"]}:
            continue
        if w.get("package") and w["package"] != finding["package"]:
            continue
        if w.get("path") and w["path"] not in finding["path"]:
            continue
        return w
    return None


def findings(trivy_image: str, image: str) -> list[dict[str, str]]:
    raw = _trivy(
        trivy_image, "image", "--quiet", "--scanners", "vuln", "--severity", "HIGH,CRITICAL", "--ignore-unfixed",
        "--format", "json", image,
    )  # fmt: skip
    return [
        {
            "id": v["VulnerabilityID"],
            "severity": v["Severity"],
            "package": v["PkgName"],
            "version": v.get("InstalledVersion", ""),
            "fixed": v.get("FixedVersion", ""),
            "path": v.get("PkgPath") or result.get("Target", ""),
        }
        for result in json.loads(raw).get("Results") or []
        for v in result.get("Vulnerabilities") or []
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: run_trivy.py <trivy-image>")
    trivy_image = argv[0]
    waivers, expired = load_waivers(datetime.now(tz=UTC).date())
    if expired:
        print(f"trivy: expired waivers need re-review: {', '.join(expired)}", file=sys.stderr)
        return 1
    for old in SBOM_DIR.glob("*.cdx.json"):
        old.unlink()  # so the SBOMs on disk are always this run's
    failed = False
    for image in GATED_IMAGES:
        name = image.split(":")[0]
        _trivy(
            trivy_image,
            "image",
            "--quiet",
            "--format",
            "cyclonedx",
            "--output",
            f"/out/{name}.cdx.json",
            image,
            out=SBOM_DIR,
        )
        gated, upstream, covered = [], [], Counter[str]()
        for f in findings(trivy_image, image):
            if UPSTREAM_SPARK in f["path"]:
                upstream.append(f)
            elif (w := waived(f, image, waivers)) is not None:
                covered[w["id"] if w["id"] != "*" else Path(w.get("path", "")).name] += 1
            else:
                gated.append(f)
        failed = failed or bool(gated)
        waived_text = ", ".join(f"{k} x{n}" for k, n in sorted(covered.items())) or "none"
        print(
            f"trivy {image}: {len(gated)} unwaived fixable HIGH/CRITICAL, waived: {waived_text}, "
            f"reported only (inside the Spark distribution jars): {len(upstream)}; "
            f"SBOM dist/sbom/{name}.cdx.json"
        )
        for f in gated:
            print(f"  GATED {f['severity']} {f['id']} {f['package']} {f['version']} -> {f['fixed']} [{f['path']}]")
    config = _trivy(
        trivy_image, "config", "--quiet", "--severity", "HIGH,CRITICAL", "--format", "json", "/repo/deploy/docker",
        mount_repo=True,
    )  # fmt: skip
    misconfigs = [
        m
        for r in json.loads(config).get("Results") or []
        for m in r.get("Misconfigurations") or []
        if m.get("Status") == "FAIL"
    ]
    print(f"trivy config deploy/docker: {len(misconfigs)} HIGH/CRITICAL misconfigurations")
    for m in misconfigs:
        print(f"  GATED {m.get('Severity')} {m.get('ID')} {m.get('Title')}")
    failed = failed or bool(misconfigs)
    for image in THIRD_PARTY:
        ref = next(
            (
                line.split()[-1]
                for line in (REPO / "tools" / "versions.lock").read_text().splitlines()
                if line.startswith(image)
            ),
            "",
        ).strip('"')
        if ref:
            n = len(findings(trivy_image, ref))
            print(f"trivy {ref.split('@')[0]}: {n} fixable HIGH/CRITICAL (third party, reported only)")
    print(f"trivy: {'FAIL' if failed else 'PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
