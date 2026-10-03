"""Run a stage gate and write docs/stages/stage-N-report.md from the real command output.

Every step runs even if an earlier one fails, so the report always shows the full picture; the gate
passes only if every step and every criterion passes. Logs are kept under .local/gate/stage-N/.
An optional hand-written summary (docs/stages/stage-N-summary.md) is placed at the top of the report.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
RUN = ["uv", "run", "--frozen"]


@dataclass
class StepResult:
    name: str
    command: str
    returncode: int
    seconds: float
    log: Path
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class Gate:
    number: str
    title: str
    steps: list[tuple[str, list[str], int]]  # (name, argv, expected return code)
    criteria: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = field(default_factory=list)


def _run_step(name: str, argv: list[str], expected: int, log_dir: Path, index: int) -> StepResult:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    log = log_dir / f"{index:02d}-{slug}.log"
    print(f"[gate] {index:02d} {name} ...", flush=True)
    start = time.monotonic()
    proc = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, check=False)
    seconds = time.monotonic() - start
    output = proc.stdout + proc.stderr
    log.write_text(f"$ {' '.join(argv)}\n{output}\n[exit {proc.returncode}]\n", encoding="utf-8")
    rc = 0 if proc.returncode == expected else (proc.returncode or 1)
    status = "ok" if rc == 0 else f"FAILED (exit {proc.returncode}, expected {expected})"
    print(f"[gate]    {status} in {seconds:.1f}s", flush=True)
    return StepResult(name, " ".join(argv), rc, seconds, log, output)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _tail(text: str, lines: int = 25) -> str:
    kept = [_ANSI.sub("", ln) for ln in text.splitlines() if ln.strip()]
    return "\n".join(kept[-lines:])


# ---------------------------------------------------------------- Gate 0 criteria helpers


def _bootstrap_idempotent(r: dict[str, StepResult]) -> tuple[bool, str]:
    a, b = r["fingerprint after 1st bootstrap"].output, r["fingerprint after 2nd bootstrap"].output
    try:
        same = json.loads(a) == json.loads(b)
    except json.JSONDecodeError:
        return False, "fingerprints were not valid JSON"
    ok = same and r["bootstrap (1st run)"].ok and r["bootstrap (2nd run)"].ok
    return ok, "fingerprints identical" if same else "fingerprints differ"


def _versions(r: dict[str, StepResult]) -> tuple[bool, str]:
    m = re.search(r"check-versions: (\d+)/(\d+) OK", r["check versions"].output)
    return (r["check versions"].ok and bool(m)), (m.group(0) if m else "no summary line")


def _static(r: dict[str, StepResult]) -> tuple[bool, str]:
    ok = r["lint (ruff, format, spark bans, vectors)"].ok and r["typecheck (mypy --strict)"].ok
    mypy = re.search(r"Success: no issues found in \d+ source files", r["typecheck (mypy --strict)"].output)
    return ok, mypy.group(0) if mypy else "mypy reported issues"


def _unit(r: dict[str, StepResult]) -> tuple[bool, str]:
    out = r["unit tests + contracts coverage"].output
    passed = re.search(r"(\d+) passed", out)
    cov = re.search(r"TOTAL\s+.*?(\d+)%", out)
    failed = re.search(r"(\d+) failed", out)
    ok = r["unit tests + contracts coverage"].ok and not failed and cov is not None and int(cov.group(1)) >= 90
    return ok, f"{passed.group(1) if passed else '?'} passed, contracts coverage {cov.group(1) if cov else '?'}%"


def _seeded(r: dict[str, StepResult]) -> tuple[bool, str]:
    out = r["banned-API lint catches seeded violations"].output
    m = re.search(r"(\d+) violation\(s\)", out)
    rules = sorted(set(re.findall(r"SB\d{3}", out)))
    ok = r["banned-API lint catches seeded violations"].ok and bool(m) and len(rules) == 11
    return ok, f"{m.group(1) if m else '?'} violations across {len(rules)} rules ({', '.join(rules)})"


def _spark(r: dict[str, StepResult]) -> tuple[bool, str]:
    out = r["host Spark tests"].output
    passed = re.search(r"(\d+) passed", out)
    xfailed = re.search(r"(\d+) xfailed", out)
    failed = re.search(r"(\d+) (failed|xpassed)", out)
    vectors = len(json.loads((REPO / "libs/contracts/src/close_contracts/test_vectors/ids.json").read_text()))
    ok = r["host Spark tests"].ok and not failed and vectors >= 50
    return ok, (
        f"{passed.group(1) if passed else '?'} passed, {xfailed.group(1) if xfailed else 0} strict-xfail canaries, "
        f"{vectors} hash-ID vectors checked in Spark"
    )


def _wheels(r: dict[str, StepResult]) -> tuple[bool, str]:
    out = r["built wheels in a clean venv"].output
    ok = r["built wheels in a clean venv"].ok and "wheel smoke: OK" in out
    digest = re.search(r"installed wheel : rows=(\d+) digest=([0-9a-f]{12})", out)
    detail = f"rows={digest.group(1)}, digest {digest.group(2)}... equals the dev env" if digest else "no result"
    return ok, detail


def _parity(r: dict[str, StepResult]) -> tuple[bool, str]:
    out = r["S2 parity x3 on SeaweedFS"].output
    runs = re.findall(r"(\d+) passed", out)
    failed = re.search(r"\d+ failed", out)
    ok = r["S2 parity x3 on SeaweedFS"].ok and len(runs) == 3 and len(set(runs)) == 1 and not failed
    feats = runs[0] if runs else "?"
    return ok, f"{len(runs)}/3 runs green, {feats} allowlisted features per run"


def _adrs(_: dict[str, StepResult]) -> tuple[bool, str]:
    needed = [
        "0001-architecture-baseline",
        "0002-spark-delta-parity-allowlist",
        "0003-local-object-store-seaweedfs",
        "0004-job-entrypoints-and-notebooks",
        "0005-read-path-typed-query-specs",
    ]
    present = [n for n in needed if list((REPO / "docs/adr").glob(f"{n}.md"))]
    return len(present) == len(needed), f"{len(present)}/{len(needed)} ADRs present"


# ---------------------------------------------------------------- Gate 1a criteria helpers

CURRENT = {"stage": "1a"}  # set by main(): which gate's JUnit reports the criteria read


def _junit_path(key: str, stage: str = "1a") -> Path:
    return REPO / ".local" / "gate" / f"stage-{stage}" / f"junit-{key}.xml"


def _junit(key: str) -> dict[str, str]:
    """``module::test`` -> passed | failed | error | skipped | xfailed, from a pytest JUnit report."""
    import xml.etree.ElementTree as ET  # nosec B405 (our own pytest output)

    outcomes: dict[str, str] = {}
    for case in ET.parse(_junit_path(key, CURRENT["stage"])).iter("testcase"):  # noqa: S314  # nosec B314
        name = f"{case.get('classname')}::{case.get('name')}"
        tags = {child.tag: child for child in case}
        if "failure" in tags:
            outcomes[name] = "failed"
        elif "error" in tags:
            outcomes[name] = "error"
        elif "skipped" in tags:
            kind = tags["skipped"].get("type", "")
            outcomes[name] = "xfailed" if "xfail" in kind else "skipped"
        else:
            outcomes[name] = "passed"
    return outcomes


def _tests(key: str, *patterns: str, want: str = "passed", at_least: int = 1) -> tuple[bool, str]:
    """Every test matching any of ``patterns`` (substring of module::test) has outcome ``want``."""
    try:
        outcomes = _junit(key)
    except (OSError, SyntaxError) as exc:  # a missing report, or a truncated one (ET.ParseError)
        return False, f"no readable JUnit report for {key}: {exc}"
    matched = {name: o for name, o in outcomes.items() if any(p in name for p in patterns)}
    bad = sorted(name.split("::", 1)[1] for name, o in matched.items() if o != want)
    ok = len(matched) >= at_least and not bad
    detail = f"{len(matched) - len(bad)}/{len(matched)} {want}"
    return ok, detail + (f"; not {want}: {', '.join(bad)}" if bad else "")


def _combine(*checks: tuple[bool, str]) -> tuple[bool, str]:
    return all(ok for ok, _ in checks), "; ".join(detail for _, detail in checks)


def _bringup(r: dict[str, StepResult]) -> tuple[bool, str]:
    step = r["bring-up: cold, warm, 3 cycles"]
    start = step.output.find("{")
    if start < 0:
        return False, "no measurement"
    data, _ = json.JSONDecoder().raw_decode(step.output[start:])
    ups = ", ".join(str(c["up_s"]) for c in data["cycles"])
    detail = (
        f"cold {data['cold_s']}s ({data['cold_downloaded_mb']} MB downloaded, no project cache), "
        f"warm {data['warm_s']}s, "
        f"cycles {ups}s, all healthy {data['cycles_healthy']}, data kept {data['data_kept']}, "
        f"smoke after cold {data['smoke_after_cold']}, after cycles {data['smoke_after_cycles']}"
    )
    return step.ok and data["verdict"] == "PASS", detail


def _smoke(r: dict[str, StepResult]) -> tuple[bool, str]:
    ok, detail = _tests("e2e", "test_03_smoke")
    timings = re.search(r"smoke timings \(s\): (\[[^\]]*\]); max ([\d.]+)", r["end-to-end suite"].output)
    if timings:
        detail += f"; per-run seconds {timings.group(1)}, max {timings.group(2)}"
        ok = ok and float(timings.group(2)) <= 120
    return ok, detail


def _memory(r: dict[str, StepResult]) -> tuple[bool, str]:
    step = r["memory verdict (sum of container peaks)"]
    line = next((ln for ln in step.output.splitlines() if ln.startswith("memory:")), "no measurement")
    return step.ok, line


def _regression(r: dict[str, StepResult]) -> tuple[bool, str]:
    checks = [
        _versions(r),
        _bootstrap_idempotent(r),
        _static(r),
        _unit(r),
        _seeded(r),
        _spark(r),
        _wheels(r),
        _parity(r),
    ]
    runs = re.findall(r"(\d+) passed", r["S2 parity x3 on SeaweedFS"].output)
    parity_file = (REPO / "engine/tests/parity/test_delta_parity.py").read_text()
    features = len(re.findall(r"^def test_\d+_", parity_file, re.MULTILINE))  # 18 until Stage 7c added ADD COLUMNS
    allowlisted = len(runs) == 3 and set(runs) == {str(features)} and features >= 18
    metadata = "commit_user_metadata" in parity_file
    ok = all(c[0] for c in checks) and allowlisted and metadata
    return ok, (
        f"{checks[0][1]} (incl. every image pin); bootstrap twice: {checks[1][1]}; seeded lint: {checks[4][1]}; "
        "lint, types, unit, Spark, wheels and parity green; "
        f"parity {features} features x 3 incl. commit userMetadata: {allowlisted}"
    )


def _jar_lock(r: dict[str, StepResult]) -> tuple[bool, str]:
    step = r["jar lock matches the engine's resolution"]
    m = re.search(r"jars\.lock (matches the resolution \(\d+ jars\)|is stale.*)", step.output)
    if m is None:
        return False, "no verdict line"
    return step.ok and "matches" in m.group(0), m.group(0)


def _security(r: dict[str, StepResult]) -> tuple[bool, str]:
    out = _ANSI.sub("", r["security (bandit, pip-audit, gitleaks)"].output)
    audit = re.search(r"No known vulnerabilities found[^\n]*", out)
    waivers = re.search(r"applying \d+ reviewed waiver\(s\): [^\n]*", out)
    leaks = "no leaks found" in out
    ok = r["security (bandit, pip-audit, gitleaks)"].ok and bool(audit) and leaks
    detail = (
        "bandit: no medium or high findings; "
        f"pip-audit: {audit.group(0) if audit else 'FAILED'}{f' ({waivers.group(0)})' if waivers else ''}; "
        f"gitleaks: {'no leaks found' if leaks else 'LEAKS FOUND'}"
    )
    return ok, detail


def _read_path(r: dict[str, StepResult]) -> tuple[bool, str]:
    step = r["read-path benchmark (100k p95, 1M and 4-client baselines)"]
    start = step.output.find("{")
    if start < 0:
        return False, "no measurement"
    data, _ = json.JSONDecoder().raw_decode(step.output[start:])

    def p95s(block: dict[str, dict[str, int]]) -> str:
        return ", ".join(f"{name} {v['p95_ms']} ms" for name, v in sorted(block.items()))

    detail = f"100k-row p95: {p95s(data['100k'])} (budget {data['budget_s']} s)"
    if "1m_baseline" in data:
        detail += f"; 1M-row p95 (baseline): {p95s(data['1m_baseline'])}"
    detail += f"; 4 concurrent clients p95 {data['concurrent_100k']['p95_ms']} ms (baseline)"
    return step.ok and data["verdict"] == "PASS", detail


def _workflows(r: dict[str, StepResult]) -> tuple[bool, str]:
    step = r["workflow lint (actionlint, zizmor)"]
    uses = [
        line.split("uses:", 1)[1].strip()
        for wf in sorted((REPO / ".github" / "workflows").glob("*.yml"))
        for line in wf.read_text().splitlines()
        if "uses:" in line
    ]
    unpinned = [u for u in uses if not re.search(r"@[0-9a-f]{40}(\s|$)", u)]
    clean = "No findings to report" in step.output
    ok = step.ok and bool(uses) and not unpinned and clean
    zizmor = "no findings" if clean else "FINDINGS"
    return ok, f"{len(uses)} `uses:` lines, unpinned: {unpinned or 'none'}; actionlint clean; zizmor: {zizmor}"


def _trivy_scan(r: dict[str, StepResult]) -> tuple[bool, str]:
    step = r["trivy (images, Dockerfiles, SBOM)"]
    lines = [ln.split("; SBOM")[0] for ln in step.output.splitlines() if ln.startswith("trivy ")]
    sboms = []
    for image in ("close-app", "close-spark"):  # run_trivy.py deletes old SBOMs first, so these are this run's
        path = REPO / "dist" / "sbom" / f"{image}.cdx.json"
        doc = json.loads(path.read_text()) if path.exists() else {}
        if doc.get("bomFormat") == "CycloneDX" and doc.get("components"):
            sboms.append(f"{path.name} ({len(doc['components'])} components)")
    ok = step.ok and "trivy: PASS" in step.output and len(sboms) == 2
    return ok, "; ".join(lines) + f"; CycloneDX SBOMs: {', '.join(sboms) or 'none'}"


def _uc_spike(r: dict[str, StepResult]) -> tuple[bool, str]:
    step = r["Unity Catalog spike (S1)"]
    m = re.search(r"uc-spike: (\d+)/(\d+) operations work", step.output)
    return step.ok and bool(m), (m.group(0) if m else "no result") + " (ADR-0010)"


def _docker_vm(_: dict[str, StepResult]) -> tuple[bool, str]:
    out = subprocess.run(["docker", "info", "--format", "{{.MemTotal}}"], capture_output=True, text=True, check=False)
    gib = int(out.stdout.strip() or 0) / 2**30
    return gib >= 9.5, f"Docker VM memory {gib:.2f} GiB (you chose to raise it to 10 GB for Stage 1b)"


def _adrs_1b(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 11)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(names), f"{len(present)}/{len(names)} ADRs present (0001-0010, incl. S3 and S1)"


def _adrs_1a(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 9)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(names), f"{len(present)}/{len(names)} ADRs present (0001-0008)"


GATES: dict[str, Gate] = {
    "0": Gate(
        number="0",
        title="Toolchain, scaffold, host-side parity",
        steps=[
            ("bootstrap (1st run)", ["make", "bootstrap"], 0),
            ("fingerprint after 1st bootstrap", [*RUN, "python", "tools/bootstrap_fingerprint.py"], 0),
            ("bootstrap (2nd run)", ["make", "bootstrap"], 0),
            ("fingerprint after 2nd bootstrap", [*RUN, "python", "tools/bootstrap_fingerprint.py"], 0),
            ("check versions", ["make", "check-versions"], 0),
            ("lint (ruff, format, spark bans, vectors)", ["make", "lint"], 0),
            ("typecheck (mypy --strict)", ["make", "typecheck"], 0),
            ("unit tests + contracts coverage", ["make", "test-unit"], 0),
            (
                "banned-API lint catches seeded violations",
                [*RUN, "python", "tools/lint_spark_bans.py", "tools/tests/fixtures/bad_spark.py"],
                1,
            ),
            ("host Spark tests", ["make", "test-spark"], 0),
            ("built wheels in a clean venv", ["make", "wheel-smoke"], 0),
            ("S2 parity x3 on SeaweedFS", ["make", "test-parity"], 0),
            ("security (bandit, pip-audit, gitleaks)", ["make", "security"], 0),
        ],
        criteria=[
            ("`make bootstrap` twice changes nothing", _bootstrap_idempotent),
            ("Installed versions match tools/versions.lock", _versions),
            ("ruff + mypy --strict report 0 errors", _static),
            ("Unit tests 100% green, contracts coverage >= 90%", _unit),
            ("Banned-API lint catches seeded violations (all 11 rules)", _seeded),
            (">= 50 hash-ID vectors identical in Python and Spark; S4 digests equal; notebooks run as scripts", _spark),
            ("Built wheels install cleanly and reproduce the dev digest (close-job console script)", _wheels),
            ("S2: every allowlisted feature passes 3/3 on the S3-compatible store", _parity),
            ("ADRs written", _adrs),
        ],
    ),
    "1a": Gate(
        number="1a",
        title="Write path and orchestration on the local Databricks-shaped stack",
        steps=[
            # Stage 0 regression first, under Gate 0's own step names (bootstrap also checks uv.lock with --locked).
            ("bootstrap (1st run)", ["make", "bootstrap"], 0),
            ("fingerprint after 1st bootstrap", [*RUN, "python", "tools/bootstrap_fingerprint.py"], 0),
            ("bootstrap (2nd run)", ["make", "bootstrap"], 0),
            ("fingerprint after 2nd bootstrap", [*RUN, "python", "tools/bootstrap_fingerprint.py"], 0),
            ("check versions", ["make", "check-versions"], 0),
            ("lint (ruff, format, spark bans, vectors)", ["make", "lint"], 0),
            ("typecheck (mypy --strict)", ["make", "typecheck"], 0),
            ("unit tests + contracts coverage", ["make", "test-unit"], 0),
            (
                "banned-API lint catches seeded violations",
                [*RUN, "python", "tools/lint_spark_bans.py", "tools/tests/fixtures/bad_spark.py"],
                1,
            ),
            ("host Spark tests", ["make", "test-spark", f"PYTEST_ARGS=--junitxml={_junit_path('spark')}"], 0),
            ("built wheels in a clean venv", ["make", "wheel-smoke"], 0),
            ("S2 parity x3 on SeaweedFS", ["make", "test-parity"], 0),
            ("jar lock matches the engine's resolution", [*RUN, "python", "tools/lock_jars.py", "--check"], 0),
            ("test databases up", ["make", "up-pg"], 0),
            (
                "control-plane library (incl. 100-interval scheduler)",
                [
                    *RUN,
                    "pytest",
                    "libs/platform/tests",
                    "-q",
                    "--timeout",
                    "900",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={_junit_path('platform')}",
                ],
                0,
            ),
            ("app (FastAPI) against Postgres", ["make", "test-app", f"PYTEST_ARGS=--junitxml={_junit_path('app')}"], 0),
            (
                "Jobs API emulator via the databricks-sdk",
                ["make", "test-emulator", f"PYTEST_ARGS=--junitxml={_junit_path('emulator')}"],
                0,
            ),
            ("test databases down", ["make", "compose-down"], 0),
            ("bring-up: cold, warm, 3 cycles", [*RUN, "python", "tools/measure_bringup.py"], 0),
            ("memory watcher start", [*RUN, "python", "tools/measure_memory.py", "start"], 0),
            ("end-to-end suite", ["make", "test-e2e", f"PYTEST_ARGS=--junitxml={_junit_path('e2e')}"], 0),
            ("memory verdict (sum of container peaks)", [*RUN, "python", "tools/measure_memory.py", "stop"], 0),
            ("security (bandit, pip-audit, gitleaks)", ["make", "security"], 0),
        ],
        criteria=[
            ("Bring-up: cold <= 10 min, warm <= 2 min, 3 down/up cycles healthy with data kept", _bringup),
            ("Smoke: 10/10 through devproxy, each <= 120 s, results scoped to each user's entities", _smoke),
            (
                "Idempotent submit: 20 deliveries -> 1 run; app killed after accepting and during run-now "
                "with TOKEN_TTL=5s -> 0 duplicates",
                lambda _: _combine(
                    _tests("e2e", "test_05_idempotent_submit", at_least=3),
                    _tests("app", "same_key", "replay", "keys_are_scoped", "failed_request_stores_nothing", at_least=4),
                    _tests("platform", "redeliver", "look", at_least=2),
                    _tests("e2e", "test_no_external_run_was_ever_duplicated"),
                ),
            ),
            (
                "Single writer: 20 parallel starts -> one 202 + nineteen 409, token +1",
                lambda _: _combine(
                    _tests("e2e", "test_04_single_writer"),
                    _tests("app", "twenty_parallel_starts"),
                    _tests("platform", "test_leases", at_least=5),
                ),
            ),
            (
                "Hand-over at 5 kill points: a new grant only after the old attempt is TERMINATED (Postgres "
                "time); a Jobs API outage (503) freezes everything",
                lambda _: _combine(
                    _tests(
                        "e2e", "test_app_killed_right_after_accepting", "test_app_killed_during_run_now", at_least=2
                    ),
                    _tests("e2e", "test_06_handover", at_least=4),
                    _tests("e2e", "test_every_grant_follows_the_previous_attempts_termination"),
                    _tests("platform", "unknown_status", "quarantine", "long_outage", at_least=3),
                ),
            ),
            (
                "Zombie: a stale token is fenced before commit and nothing is committed; residual window is a "
                "strict xfail",
                lambda _: _combine(
                    _tests("e2e", "test_08_zombie"),
                    _tests(
                        "spark", "test_highest_token", "test_stale_token", "test_newer_claim_while_zombie", at_least=3
                    ),
                    _tests("spark", "residual_window", want="xfailed"),
                ),
            ),
            (
                "Cancel <= 30 s, timeout <= timeout_seconds + 10 s, no orphan processes",
                lambda _: _tests("e2e", "test_07_cancel_timeout", at_least=2),
            ),
            (
                "Outbox: 100 events, 3 crash points -> 0 lost, 0 double-applied; submit/cancel never dead-letter",
                lambda _: _combine(
                    _tests("platform", "test_outbox_faults", at_least=4),
                    _tests("platform", "never_dead_letter", "other_topics", at_least=2),
                    _tests("e2e", "test_the_outbox_lost_nothing"),
                ),
            ),
            (
                "Scheduler: 2 instances x 100 intervals, each tick exactly once; survivor within interval + 1 s "
                "after SIGKILL or pause",
                lambda _: _combine(
                    _tests("platform", "test_scheduler_claims", at_least=7),
                    _tests("e2e", "test_09_scheduler_ha", at_least=2),
                ),
            ),
            (
                "Identity: 401 without headers; every spoof variant yields only the proxy identity; only devproxy "
                "is published",
                lambda _: _combine(
                    _tests("e2e", "test_01_identity", at_least=10), _tests("app", "test_identity", at_least=8)
                ),
            ),
            (
                "Least privilege: S3 writer key only in jobs (faults is the named exception); app has no S3 keys; "
                "jobs never see emulator credentials",
                lambda _: _combine(
                    _tests("e2e", "test_02_least_privilege", at_least=4),
                    _tests("emulator", "never_see_the_emulator_credentials"),
                ),
            ),
            (
                "Migrations up/down/up; readiness enforces the schema head; bundle rejects unknown keys and "
                "target overrides",
                lambda _: _combine(
                    _tests("app", "test_migrations", "readiness", "schema", at_least=4),
                    _tests("e2e", "test_91_readiness"),
                    _tests("emulator", "test_bundle", at_least=3),
                ),
            ),
            ("Memory: sum of container peaks <= 6.5 GiB, oom_kill = 0", _memory),
            ("Regression: Stage 0 steps green; parity allowlist includes Delta commit userMetadata", _regression),
            ("Jar lock matches what the engine resolves", _jar_lock),
            ("Security: bandit, pip-audit, gitleaks clean", _security),
            ("ADRs written (0001-0008)", _adrs_1a),
        ],
    ),
}


def _1b_steps(stage: str) -> list[tuple[str, list[str], int]]:
    return [
        ("bootstrap (1st run)", ["make", "bootstrap"], 0),
        ("fingerprint after 1st bootstrap", [*RUN, "python", "tools/bootstrap_fingerprint.py"], 0),
        ("bootstrap (2nd run)", ["make", "bootstrap"], 0),
        ("fingerprint after 2nd bootstrap", [*RUN, "python", "tools/bootstrap_fingerprint.py"], 0),
        ("check versions", ["make", "check-versions"], 0),
        ("lint (ruff, format, spark bans, vectors)", ["make", "lint"], 0),
        ("typecheck (mypy --strict)", ["make", "typecheck"], 0),
        ("workflow lint (actionlint, zizmor)", ["make", "lint-workflows"], 0),
        ("unit tests + contracts coverage", ["make", "test-unit"], 0),
        (
            "banned-API lint catches seeded violations",
            [*RUN, "python", "tools/lint_spark_bans.py", "tools/tests/fixtures/bad_spark.py"],
            1,
        ),
        ("host Spark tests", ["make", "test-spark", f"PYTEST_ARGS=--junitxml={_junit_path('spark', stage)}"], 0),
        ("built wheels in a clean venv", ["make", "wheel-smoke"], 0),
        ("S2 parity x3 on SeaweedFS", ["make", "test-parity"], 0),
        ("jar lock matches the engine's resolution", [*RUN, "python", "tools/lock_jars.py", "--check"], 0),
        ("test databases up", ["make", "up-pg"], 0),
        (
            "control-plane library and read path (incl. slow: 100 intervals, 2,000 and 10,000 specs)",
            [
                *RUN,
                "pytest",
                "libs/platform/tests",
                "-q",
                "--timeout",
                "3600",
                "-p",
                "no:cacheprovider",
                f"--junitxml={_junit_path('platform', stage)}",
            ],
            0,
        ),
        (
            "app (FastAPI) against Postgres",
            ["make", "test-app", f"PYTEST_ARGS=--junitxml={_junit_path('app', stage)}"],
            0,
        ),
        (
            "Jobs API emulator via the databricks-sdk",
            ["make", "test-emulator", f"PYTEST_ARGS=--junitxml={_junit_path('emulator', stage)}"],
            0,
        ),
        ("test databases down", ["make", "compose-down"], 0),
        ("bring-up: cold, warm, 3 cycles", [*RUN, "python", "tools/measure_bringup.py"], 0),
        ("trivy (images, Dockerfiles, SBOM)", ["make", "scan"], 0),
        ("memory watcher start", [*RUN, "python", "tools/measure_memory.py", "start"], 0),
        ("read-path benchmark (100k p95, 1M and 4-client baselines)", ["make", "read-benchmark"], 0),
        ("Unity Catalog spike (S1)", ["make", "uc-spike"], 0),
        ("end-to-end suite", ["make", "test-e2e", f"PYTEST_ARGS=--junitxml={_junit_path('e2e', stage)}"], 0),
        (
            "memory verdict (sum of container peaks)",
            [*RUN, "python", "tools/measure_memory.py", "stop", "--budget-gib", "8.5"],
            0,
        ),
        ("security (bandit, pip-audit, gitleaks)", ["make", "security"], 0),
    ]


_1B_STEPS = _1b_steps("1b")

_1B_CRITERIA: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = [
    ("Read path: 100k rows, 3 requests x 100 after 5 warm-ups, p95 <= 1.5 s (1M and 4 clients recorded)", _read_path),
    (
        "Entity filter: 2,000 generated specs return 0 rows outside the caller's entities",
        lambda _: _combine(
            _tests("platform", "test_2000_generated_specs_never_return_rows_outside"),
            _tests("platform", "test_query_engine", at_least=10),
        ),
    ),
    (
        "Renderer: 10,000 sentinel specs bind every value, markers 1:1, parse as Databricks SQL, entity predicate "
        "top-level; 300 specs identical on plain Spark, the DataFrame backend and the reference",
        lambda _: _combine(
            _tests("platform", "test_rendered_sql_binds_every_caller_value_10k"),
            _tests(
                "platform",
                "test_three_executors_agree_on_300_specs",
                "test_dataframe_backend_matches_the_reference",
                at_least=2,
            ),
        ),
    ),
    (
        "Smoke: 10/10 with each user's per-entity digests read back through a QuerySpec; results, totals and "
        "rows agree",
        lambda r: _combine(_smoke(r), _tests("e2e", "test_manifest_totals_and_paged_rows_agree")),
    ),
    (
        "Read path through the API: cursors bound to the user and the query, scoped totals, manifest integrity",
        lambda _: _combine(
            _tests("app", "test_read_path", at_least=10), _tests("e2e", "test_a_tampered_or_borrowed_cursor")
        ),
    ),
    (
        "Least privilege: a write through Spark Connect fails with 403; the reader key only in spark-connect, the "
        "cursor key only in the app",
        lambda _: _combine(
            _tests("e2e", "test_a_write_through_spark_connect_is_refused_with_403"),
            _tests("e2e", "reader_key_is_only_in_spark_connect", "cursor_signing_key_is_only_in_the_app", at_least=2),
        ),
    ),
    ("Catalog: spike S1 run against Unity Catalog OSS 0.6.0, results in ADR-0010", _uc_spike),
    ("CI: every `uses:` pinned to a commit SHA; actionlint and zizmor clean", _workflows),
    ("Security: trivy 0 unwaived fixable critical/high on our images and Dockerfiles; SBOMs written", _trivy_scan),
    ("Memory: sum of container peaks <= 8.5 GiB, oom_kill = 0", _memory),
    ("Docker VM raised to about 10 GB", _docker_vm),
    ("ADRs written (0001-0010)", _adrs_1b),
]


def _gate_1b() -> Gate:
    regression = [
        (f"Regression (1a): {name.removeprefix('Regression: ')}", check)
        for name, check in GATES["1a"].criteria
        if not name.startswith(("Memory:", "ADRs written"))  # replaced by the 1b budget and ADR list above
    ]
    return Gate(
        number="1b",
        title="Read path, catalog and CI on the local Databricks-shaped stack",
        steps=_1B_STEPS,
        criteria=[*_1B_CRITERIA, *regression],
    )


GATES["1b"] = _gate_1b()


def _2a_steps() -> list[tuple[str, list[str], int]]:
    steps = []
    for name, argv, expected in _1b_steps("2a"):
        command = argv
        if name == "unit tests + contracts coverage":
            command = ["make", "test-unit", f"PYTEST_ARGS=--junitxml={_junit_path('unit', '2a')}"]
        steps.append((name, command, expected))
        if name == "typecheck (mypy --strict)":
            steps.append(
                ("JSON Schema snapshots (drift check)", [*RUN, "python", "tools/gen_schemas.py", "--check"], 0)
            )
    return steps


def _schemas(r: dict[str, StepResult]) -> tuple[bool, str]:
    step = r["JSON Schema snapshots (drift check)"]
    m = re.search(r"schemas: (\d+) snapshots, 0 drift", step.output)
    return step.ok and bool(m), m.group(0) if m else step.output.strip().splitlines()[-1:][
        0
    ] if step.output.strip() else "no output"


def _headroom(container: str, label: str) -> Callable[[dict[str, StepResult]], tuple[bool, str]]:
    """A container's peak process memory (non-reclaimable) must stay at or below 85% of its limit."""

    def check(r: dict[str, StepResult]) -> tuple[bool, str]:
        step = r["memory verdict (sum of container peaks)"]
        line = next((ln for ln in step.output.splitlines() if ln.startswith("memory:")), "")
        m = re.search(rf"{container}-1 \d+ of (\d+) MiB \(process max (\d+)\)", line)
        if m is None:
            return False, f"no {container} measurement"
        limit, process = int(m.group(1)), int(m.group(2))
        share = process / limit
        return share <= 0.85, f"{label} process peak {process} of {limit} MiB ({share:.0%}; at most 85% allowed)"

    return check


_connect_headroom = _headroom("spark-connect", "Spark Connect")


def _domain_docs(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = ["marketplace-lifecycle-and-money-flows", "glossary", "blackline-concept-map", "accounting-policy-sheet"]
    present = [n for n in names if (REPO / "docs" / "domain" / f"{n}.md").is_file()]
    return len(present) == len(names), f"{len(present)}/{len(names)} present: {', '.join(present)}"


def _adrs_2a(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 12)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(names), f"{len(present)}/{len(names)} ADRs present (0001-0011, incl. the DSL)"


_2A_CRITERIA: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = [
    (
        "Configuration models: 1,000 generated examples per model round-trip exactly through canonical JSON",
        lambda _: _tests("unit", "round_trip", at_least=5),
    ),
    ("JSON Schema snapshots show 0 drift", _schemas),
    (
        "DSL typecheck: a fixed corpus types correctly or fails with stable codes and paths; depth and size limits",
        lambda _: _tests("unit", "test_dsl_typecheck", at_least=40),
    ),
    (
        "Regex subset: unsafe and non-portable patterns rejected with codes; every accepted pattern identical in "
        "Java (Spark) and Python",
        lambda _: _combine(
            _tests("unit", "test_dsl_regex", at_least=70),
            _tests("spark", "test_every_accepted_pattern_behaves_the_same_in_java_and_python"),
        ),
    ),
    (
        "Interpreter: Spark (ANSI) semantics, SQL nulls, rejected rows and overflow, confirmed on Spark",
        lambda _: _combine(
            _tests("unit", "test_dsl_interpret", at_least=60),
            _tests("spark", "test_spark_behaviours_the_interpreter_copies", "trim_matches_spark", at_least=2),
        ),
    ),
    (
        "Configuration rules: maker-checker, content hash, M:M exact key, blocking, owners",
        lambda _: _tests(
            "unit",
            "test_match_set_rules",
            "test_condition_rules",
            "test_data_source_rules",
            "test_envelope_rules",
            "tampered",
            at_least=25,
        ),
    ),
    ("Spark Connect memory headroom (Stage 2a first task)", _connect_headroom),
    ("Domain spec: lifecycle and money flows, glossary, BlackLine concept map, policy sheet", _domain_docs),
    ("Memory: sum of container peaks <= 8.5 GiB, oom_kill = 0", _memory),
    ("Docker VM raised to about 10 GB", _docker_vm),
    ("ADRs written (0001-0011)", _adrs_2a),
]


def _gate_2a() -> Gate:
    regression = [
        (f"Regression (1b): {name.removeprefix('Regression (1a): ')}", check)
        if not name.startswith("Regression (1a)")
        else (name, check)
        for name, check in GATES["1b"].criteria
        if not name.startswith(("Memory:", "ADRs written", "Docker VM"))
    ]
    return Gate(
        number="2a",
        title="Domain, configuration contracts and the rule DSL",
        steps=_2a_steps(),
        criteria=[*_2A_CRITERIA, *regression],
    )


GATES["2a"] = _gate_2a()


def _2b_steps(stage: str = "2b") -> list[tuple[str, list[str], int]]:
    steps = []
    for name, argv, expected in _1b_steps(stage):
        command = argv
        if name == "unit tests + contracts coverage":
            command = ["make", "test-unit", f"PYTEST_ARGS=--junitxml={_junit_path('unit', stage)}"]
        steps.append((name, command, expected))
        if name == "typecheck (mypy --strict)":
            steps.append(
                ("JSON Schema snapshots (drift check)", [*RUN, "python", "tools/gen_schemas.py", "--check"], 0)
            )
        if name == "host Spark tests":
            steps.append(
                ("generator determinism (seeds x runs, local[1] vs local[4])", ["make", "datagen-determinism"], 0)
            )
        if name == "memory watcher start":
            steps.append(("migrations and generator through the stack", ["make", "datagen-stack"], 0))
    return steps


def _json_line(step: StepResult) -> dict[str, Any]:
    line = next((ln for ln in reversed(step.output.splitlines()) if ln.startswith("{")), "{}")
    data: dict[str, Any] = json.loads(line)
    return data


def _stack(r: dict[str, StepResult]) -> dict[str, Any]:
    return _json_line(r["migrations and generator through the stack"])


def _ddl(r: dict[str, StepResult]) -> tuple[bool, str]:
    data = _stack(r)
    checks = data.get("checks", {})
    ddl = data.get("ddl_apply", {})
    ok = bool(checks.get("ddl_apply_fresh")) and bool(checks.get("ddl_apply_rerun_applies_nothing"))
    return (
        ok,
        f"empty lake prefix: {ddl.get('seconds')} s, migrations {ddl.get('applied')}; "
        f"rerun applied {ddl.get('rerun_applied')}",
    )


def _timing(r: dict[str, StepResult]) -> tuple[bool, str]:
    data = _stack(r)
    checks = data.get("checks", {})
    small, medium = data.get("small", {}), data.get("medium", {})
    ok = bool(checks.get("small_within_3_min")) and bool(checks.get("medium_within_25_min"))
    return ok, (
        f"small {small.get('seconds')} s ({small.get('landing_rows')} landing rows), "
        f"medium {medium.get('seconds')} s ({medium.get('landing_rows')} landing rows), as jobs through the stack"
    )


def _break_coverage(r: dict[str, StepResult]) -> tuple[bool, str]:
    breaks = _stack(r).get("small", {}).get("breaks", {})
    unit_ok, unit_detail = _combine(
        _tests("unit", "test_small_has_every_break_type_at_least_25_times", at_least=3),
        _tests("unit", "test_order_break_counts_equal_their_quotas", "test_quota_is_exact_and_seeded", at_least=2),
    )
    ok = unit_ok and len(breaks) == 13 and min(breaks.values(), default=0) >= 25
    low = min(breaks.items(), key=lambda kv: kv[1]) if breaks else ("none", 0)
    return (
        ok,
        f"{len(breaks)} break types in the stack small run, lowest {low[0]} {low[1]}; exact quotas: {unit_detail}",
    )


def _determinism(r: dict[str, StepResult]) -> tuple[bool, str]:
    data = _json_line(r["generator determinism (seeds x runs, local[1] vs local[4])"])
    seeds = data.get("seeds", {})
    detail = "; ".join(
        f"seed {s}: {v['runs']} runs identical {v['identical_across_runs']}, "
        f"local[1] = local[4] {v['local1_equals_local4']}"
        for s, v in sorted(seeds.items())
    )
    return data.get("verdict") == "PASS", f"{detail}; seeds differ {data.get('seeds_differ')}"


_POLICY_SECTIONS = (
    "When revenue and the seller payable are booked", "Agent or owned inventory", "Shipping charged to buyers",
    "Buyer incentives", "Presentation", "Failed authentication", "Chargebacks and the reserve", "Carrier charges",
    "Store credit and gift cards", "Sales tax", "Foreign exchange", "Sign-off",
)  # fmt: skip


def _policy_sheet(_: dict[str, StepResult]) -> tuple[bool, str]:
    """The sheet is complete, and the user's confirmation that it went (or will go) to the controller is recorded."""
    sheet = REPO / "docs" / "domain" / "accounting-policy-sheet.md"
    status = REPO / "docs" / "domain" / "policy-sheet-status.md"
    text = sheet.read_text(encoding="utf-8") if sheet.exists() else ""
    present = [name for name in _POLICY_SECTIONS if f"## {name}" in text]
    record = status.read_text(encoding="utf-8").strip().splitlines() if status.exists() else []
    confirmed = next((ln for ln in record if ln.lower().startswith("confirmed")), "")
    detail = f"{len(present)}/{len(_POLICY_SECTIONS)} sections; " + (
        confirmed or "your confirmation is not recorded yet"
    )
    return len(present) == len(_POLICY_SECTIONS) and bool(confirmed), detail


def _stack_totals(r: dict[str, StepResult]) -> tuple[bool, str]:
    checks = _stack(r).get("checks", {})
    stack_ok = bool(checks.get("small_within_3_min")) and bool(checks.get("medium_within_25_min"))
    spark_ok, spark_detail = _tests("spark", "test_the_tiny_profile_writes_every_landing_table_and_the_totals_match")
    return (
        stack_ok and spark_ok,
        f"small and medium through the stack match the manifest: {stack_ok}; tiny on the host: {spark_detail}",
    )


def _adrs_2b(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 13)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(names), f"{len(present)}/{len(names)} ADRs present (0001-0012, incl. the generator)"


_2B_CRITERIA: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = [
    ("Migrations: ddl_apply on an empty lake root in 60 s or less, and a rerun applies nothing", _ddl),
    (
        "Table contracts: every registered table matches its contract (names, types, nullability, partitions, "
        "append-only); an edited migration is refused; V001 keeps the Stage 1 tables identical",
        lambda _: _tests(
            "spark",
            "test_every_registered_table_matches_its_contract",
            "test_an_edited_migration_is_refused",
            "test_v001_keeps_the_stage_1_tables_identical",
            "test_ddl_apply_is_fast",
            at_least=4,
        ),
    ),
    (
        "Default data sources: every one binds, and maps every generated row to amounts equal to the control totals",
        lambda _: _tests(
            "unit", "test_every_default_source_binds", "test_every_default_source_maps_every_generated_row", at_least=14
        ),
    ),
    (
        "Determinism: seeds 11, 23 and 47 give identical landing digests across 3 runs and local[1] vs local[4]",
        _determinism,
    ),
    ("Timing: small in 3 minutes or less and medium in 25 minutes or less, as jobs through the stack", _timing),
    (
        "Breaks: every break type at least 25 times in small, by exact quota (so within 10 percent of its rate)",
        _break_coverage,
    ),
    (
        "Answer key: 100% of its self-consistency checks pass for seeds 11, 23 and 47",
        lambda _: _tests("unit", "test_the_answer_key_passes_every_self_consistency_check", at_least=3),
    ),
    ("Control totals: the landing tables read back equal the generator's manifest", _stack_totals),
    ("JSON Schema snapshots show 0 drift (incl. the table registry and the default data sources)", _schemas),
    ("Policy sheet complete, and your confirmation that it went to your controller recorded", _policy_sheet),
    ("Memory: sum of container peaks <= 8.5 GiB, oom_kill = 0 (the medium generator included)", _memory),
    ("Jobs container memory headroom with the medium generator", _headroom("jobs", "Jobs container")),
    ("SeaweedFS memory headroom (Stage 2b first task)", _headroom("seaweedfs", "SeaweedFS")),
    ("Docker VM raised to about 10 GB", _docker_vm),
    ("ADRs written (0001-0012)", _adrs_2b),
]


def _gate_2b() -> Gate:
    regression = [
        (name if name.startswith("Regression (") else f"Regression (2a): {name}", check)
        for name, check in GATES["2a"].criteria
        if not name.startswith(("Memory:", "ADRs written", "Docker VM", "JSON Schema snapshots"))
    ]
    return Gate(
        number="2b",
        title="Schemas, migrations and the seeded marketplace generator",
        steps=_2b_steps(),
        criteria=[*_2B_CRITERIA, *regression],
    )


GATES["2b"] = _gate_2b()


# ---------------------------------------------------------------- Stage 3, reference slice

_REF = "3-ref"
_SCORES = "template scores (small 11, 23, 47; medium 11)"
_PROPERTIES = "reference matcher invariants (10,000 examples)"


def _3ref_steps(stage: str = _REF) -> list[tuple[str, list[str], int]]:
    steps = []
    for step in _2b_steps(stage):
        steps.append(step)
        if step[0] == "unit tests + contracts coverage":
            junit = _junit_path("reference", stage)
            steps.append(
                ("reference matcher tests + coverage", ["make", "test-reference", f"PYTEST_ARGS=--junitxml={junit}"], 0)
            )
            properties = [
                "env",
                "CLOSE_REFERENCE_EXAMPLES=10000",
                *RUN,
                "pytest",
                "engine/tests/reference/test_properties.py",
                "-q",
                "-p",
                "no:cacheprovider",
                "--hypothesis-show-statistics",
                f"--junitxml={_junit_path('properties', stage)}",
            ]
            steps.append((_PROPERTIES, properties, 0))
            steps.append((_SCORES, [*RUN, "python", "tools/reference_scores.py"], 0))
    return steps


def _reference_suite(r: dict[str, StepResult]) -> tuple[bool, str]:
    step = r["reference matcher tests + coverage"]
    m = re.search(r"Total coverage: ([0-9.]+)%", step.output)
    ok, detail = _tests("reference", "tests.reference", at_least=60)
    return ok and step.ok and bool(m), f"{detail}; coverage {m.group(1) if m else '?'}% (at least 90% enforced)"


def _invariants(r: dict[str, StepResult]) -> tuple[bool, str]:
    m = re.search(r"(\d+) passing, (\d+) failing", r[_PROPERTIES].output)
    ok, detail = _tests("properties", "test_reference_matcher_invariants", at_least=1)
    passing = int(m.group(1)) if m else 0
    return ok and passing >= 10_000 and m is not None and m.group(2) == "0", f"{detail}; {passing} examples passing"


def _scores(r: dict[str, StepResult]) -> dict[str, Any]:
    return _json_line(r[_SCORES])


def _scores_correct(r: dict[str, StepResult]) -> tuple[bool, str]:
    data = _scores(r)
    parts, ok = [], data.get("verdict") == "PASS" and len(data.get("runs", {})) == 4
    for spec, run in sorted(data.get("runs", {}).items()):
        families = run["families"].values()
        false_auto = sum(f["false_auto"] for f in families)
        recall = min(f["recall"] for f in families)
        ok = ok and false_auto == 0 and recall >= 0.99
        parts.append(
            f"{spec}: {run['items']:,} items in {run['seconds']} s, false automatic {false_auto}, min recall {recall}"
        )
    return ok, "; ".join(parts) + f"; peak {data.get('peak_rss_mib')} MiB"


def _scores_outcomes(r: dict[str, StepResult]) -> tuple[bool, str]:
    data = _scores(r)
    tallies: dict[str, list[int]] = {}
    groups = [0, 0]
    for run in data.get("runs", {}).values():
        for family in run["families"].values():
            for name, t in family["breaks"].items():
                tally = tallies.setdefault(name, [0, 0])
                tally[0] += t["reached"]
                tally[1] += t["total"]
            groups[0] += sum(family["reached"].values())
            groups[1] += sum(family["expected"].values())
    ok = data.get("verdict") == "PASS" and bool(tallies) and all(a == b for a, b in tallies.values())
    breaks = ", ".join(f"{name} {a}/{b}" for name, (a, b) in sorted(tallies.items()))
    return ok, f"breaks across the 4 runs: {breaks}; all expected groups reached {groups[0]:,}/{groups[1]:,}"


def _adrs_3ref(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 14)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(
        names
    ), f"{len(present)}/{len(names)} ADRs present (0001-0013, incl. the matcher semantics)"


_3REF_CRITERIA: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = [
    (
        "Correctness: templates 1, 2, 3, 6, 9 and 10 on small seeds 11, 23 and 47 and medium seed 11 give 0 false "
        "automatic matches and clean recall of at least 99% (keyed rules) in every family",
        _scores_correct,
    ),
    ("Breaks: 100% of injected breaks, and every other expected outcome, reached in every run", _scores_outcomes),
    (
        "Reference matcher: conditions, subset search, waterfall and golden tests pass, coverage at least 90%",
        _reference_suite,
    ),
    (
        "Invariants: 10,000 random match sets keep every matcher invariant (one holding per item, groups re-check "
        "against their rules, no group mixes entities, shuffled input decides the same)",
        _invariants,
    ),
    (
        "Determinism and golden digests: shuffled input decides identically, tiny seed 11 matches its pinned "
        "digests, and a carrier correction equal to its quote is suggested",
        lambda _: _tests(
            "reference",
            "test_input_order_does_not_change_any_decision",
            "test_tiny_decisions_match_the_golden_digests",
            "test_a_carrier_correction_equal_to_the_quote",
            at_least=3,
        ),
    ),
    (
        "Templates: 12 valid match sets over the default data sources, each scored by exactly one family, and "
        "frozen in the JSON Schema snapshots",
        lambda r: _combine(_tests("unit", "test_templates", at_least=3), _schemas(r)),
    ),
    ("ADRs written (0001-0013)", _adrs_3ref),
]


def _gate_3ref() -> Gate:
    regression = [
        (name if name.startswith("Regression (") else f"Regression (2b): {name}", check)
        for name, check in GATES["2b"].criteria
        if not name.startswith(("ADRs written", "Policy sheet"))
    ]
    return Gate(
        number=_REF,
        title="Stage 3 reference slice: the reference matcher, the template match sets and their golden tests",
        steps=_3ref_steps(),
        criteria=[*_3REF_CRITERIA, *regression],
    )


GATES[_REF] = _gate_3ref()


# ---------------------------------------------------------------- Stage 4: the governance kernel


def _adrs_4(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 15)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(names), f"{len(present)}/{len(names)} ADRs present (0001-0014, incl. governance)"


_4_CRITERIA: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = [
    (
        "Authorization matrix: 12 roles x 3 scopes x every operation, 0 unexpected allows and 0 unexpected denies "
        "against a hand-written expectation table, and the matrix covers every operation served",
        lambda _: _tests("app", "test_authorization_matrix", "test_the_matrix_covers_every_operation", at_least=2),
    ),
    (
        "Route inventory: every operation declares its access, every POST requires Idempotency-Key, every "
        "authenticated read is reviewed for entity scoping, only health and readiness are public",
        lambda _: _tests("app", "test_route_inventory", at_least=5),
    ),
    (
        "Separation of duties: approving your own configuration change, activating your own rule, an editor "
        "approving, and granting yourself a role are all blocked and audited",
        lambda _: _combine(
            _tests("app", "test_sod_", at_least=6), _tests("app", "test_nobody_can_grant_themselves_a_role")
        ),
    ),
    (
        "Audit chain: contiguous appends, exact round trips, nothing from a rolled-back change, no forks or gaps "
        "under 8 concurrent writers, every tamper detected; the recomputed chain is a strict xfail",
        lambda _: _combine(
            _tests(
                "app",
                "test_appends_are_contiguous_and_verify",
                "test_payloads_round_trip_exactly",
                "test_payloads_that_would_not_round_trip_are_refused",
                "test_a_rolled_back_change_leaves_no_event_and_no_gap",
                "test_concurrent_appends_never_fork_or_gap",
                "test_tampering_as_the_migrator_is_detected",
                "test_the_cli_verifies_and_reports",
                at_least=16,
            ),
            _tests("app", "test_a_recomputed_chain_is_detected", want="xfailed"),
        ),
    ),
    (
        "Audit completeness: every state change writes its audit events in its own transaction (one per change, two "
        "when an activation retires the active version), and a replay writes none",
        lambda _: _tests(
            "app",
            "test_every_post_replays_byte_identically_without_a_second_effect",
            "test_an_edit_writes_one_audit_event",
            at_least=2,
        ),
    ),
    (
        "Multi-entity objects: reading a match set that spans several entities needs a role in any one of them, "
        "and every change needs the role in all of them",
        lambda _: _tests("app", "test_a_multi_entity_match_set_is_read_with_one_entity_and_changed_with_all"),
    ),
    (
        "Idempotency: every POST replays byte-identically with no second effect, concurrent duplicates have one "
        "effect, and a key is bound to its body and its path",
        lambda _: _tests("app", "test_idempotency_all", at_least=3),
    ),
    (
        "ETags: 428 without If-Match, 412 when stale, a new ETag on every change, and a replay after a later "
        "change returns the stored response",
        lambda _: _tests(
            "app",
            "test_if_match_is_required_and_must_be_current",
            "test_a_replay_after_a_later_change_returns_the_stored_response",
            at_least=2,
        ),
    ),
    (
        "Maker-checker end to end: a data source and a match set from draft to active through the API and through "
        "the stack as the dev users, every step audited; the stack's audit chain verifies after the whole suite",
        lambda _: _combine(
            _tests("app", "test_a_match_set_goes_from_draft_to_active_and_is_superseded"),
            _tests(
                "e2e",
                "test_maker_checker_through_the_stack_and_the_chain_verifies",
                "test_the_audit_chain_verifies_after_the_whole_suite",
                at_least=2,
            ),
        ),
    ),
    (
        "Migration 0002: up, down and up clean with the privilege rules (append-only audit, no deleted assignments "
        "or versions); entity grants survive a downgrade and upgrade",
        lambda _: _tests("app", "test_migrations", at_least=2),
    ),
    (
        "Outbox: exactly once under relay crashes (built in 1a)",
        lambda _: _combine(
            _tests("platform", "test_outbox_faults", at_least=4), _tests("e2e", "test_the_outbox_lost_nothing")
        ),
    ),
    ("ADRs written (0001-0014)", _adrs_4),
]


def _gate_4() -> Gate:
    regression = [
        (name if name.startswith("Regression (") else f"Regression (3-ref): {name}", check)
        for name, check in GATES[_REF].criteria
        if not name.startswith("ADRs written")
    ]
    return Gate(
        number="4",
        title="The governance kernel: roles, entity scopes, separation of duties, the audit chain, maker-checker "
        "configuration versions",
        steps=_3ref_steps("4"),
        criteria=[*_4_CRITERIA, *regression],
    )


GATES["4"] = _gate_4()


# ---------------------------------------------------------------- Stage 6, first slice: journaling on sample inputs

_JOURNAL = "6-ref"
_JOURNALING_SCHEMAS = ("event_type", "auto_journal_definition", "account_mapping", "chart_of_accounts", "dimension_set")
_JOURNAL_SCORES = "journaling results (tiny 11 and small 11, sample configuration)"


def _6ref_steps(stage: str = _JOURNAL) -> list[tuple[str, list[str], int]]:
    steps = []
    for step in _3ref_steps(stage):
        steps.append(step)
        if step[0] == _SCORES:
            steps.append((_JOURNAL_SCORES, [*RUN, "python", "tools/journal_scores.py"], 0))
    return steps


def _journal_runs(r: dict[str, StepResult]) -> tuple[bool, dict[str, Any]]:
    data = _json_line(r[_JOURNAL_SCORES])
    return data.get("verdict") == "PASS" and len(data.get("runs", {})) == 2, data.get("runs", {})


def _journal_books(r: dict[str, StepResult]) -> tuple[bool, str]:
    ok, runs = _journal_runs(r)
    parts = []
    for spec, run in sorted(runs.items()):
        counts, o = run["counts"], run["oracle"]
        ok = ok and o["equal"] and not run["failed_types"]
        parts.append(
            f"{spec}: {run['events']:,} events to {run['lines']:,} lines in {run['book_seconds']} s "
            f"({counts.get('posted', 0):,} posted, {counts.get('no_entry', 0):,} no entry, "
            f"{run['suspense_lines']} suspense lines, {run['rounding_lines']} rounding lines); engine equals oracle "
            f"on {o['net_keys']:,} net keys ({o['differing_transaction']} transaction and "
            f"{o['differing_functional']} functional differ)"
        )
        if "matrix" in run:
            m = run["matrix"]
            ok = ok and m["policies"] == 212 and not m["mismatched"]
            parts.append(f"{m['policies']} policies on {spec} in {m['seconds']} s, {len(m['mismatched'])} mismatched")
    return ok, "; ".join(parts)


def _journal_guards(r: dict[str, StepResult]) -> tuple[bool, str]:
    ok, runs = _journal_runs(r)
    guards = [(spec, run["flip_guard"]) for spec, run in runs.items() if "flip_guard" in run]
    parts = []
    for spec, g in guards:
        ok = ok and g["flips"] == 32 and not g["unchanged"] and not g["mismatched"]
        parts.append(
            f"{spec}: {g['flips'] - len(g['unchanged'])}/{g['flips']} flips change the books, and the engine "
            f"equals the oracle under {g['flips'] - len(g['mismatched'])}/{g['flips']} ({g['seconds']} s)"
        )
    return ok and bool(guards), "; ".join(parts) or "no flip guard in the results"


def _journaling_schemas(r: dict[str, StepResult]) -> tuple[bool, str]:
    folder = REPO / "libs" / "contracts" / "src" / "close_contracts" / "schemas"
    missing = [n for n in _JOURNALING_SCHEMAS if not (folder / f"{n}.schema.json").is_file()]
    ok, detail = _schemas(r)
    found = f"; missing: {', '.join(missing)}" if missing else "; 5 journaling models frozen"
    return ok and not missing, detail + found


def _adrs_6ref(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 16)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(names), f"{len(present)}/{len(names)} ADRs present (0001-0015, incl. journaling)"


_6REF_CRITERIA: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = [
    (
        "Engine equals oracle: every sample event on tiny and small, in transaction and functional amounts, and "
        "under all 212 policy combinations on tiny; the oracle never reads the definitions or mappings",
        lambda r: _combine(
            _tests(
                "reference",
                "test_the_engine_books_what_the_oracle_books",
                "test_the_engine_equals_the_oracle_for_every_policy_combination",
                at_least=2,
            ),
            _journal_books(r),
        ),
    ),
    (
        "Guards: each of the 32 single-switch flips changes the books on small and the engine equals the oracle "
        "under each, and the data has every event type in every program",
        lambda r: _combine(
            _tests(
                "reference",
                "test_flipping_any_switch_changes_the_books_and_the_engine_follows",
                "test_the_data_has_every_event_type_in_every_program",
                at_least=2,
            ),
            _journal_guards(r),
        ),
    ),
    (
        "Invariants: every event version balances in transaction and functional currency, every balance sheet "
        "account ties to its source of truth at 4 cutoffs with the injected breaks as named reconciling items, and "
        "every source row is accounted for (read = events + excluded + rejected)",
        lambda _: _tests(
            "reference",
            "test_every_balance_ties_to_its_source_of_truth_at_each_cutoff",
            "test_every_source_row_is_accounted_for",
            at_least=2,
        ),
    ),
    (
        "Data and configuration problems: a negative amount, a missing mapping row, a missing dimension and a broken "
        "identity go to suspense with the intended account; an imbalance with the identities holding fails its "
        "event type; the circuit breaker trips above 0.5%",
        lambda _: _tests(
            "reference",
            "test_a_negative_amount_keeps_its_effect_in_suspense",
            "test_a_missing_mapping_row_records_the_key_it_looked_for",
            "test_a_missing_required_dimension_goes_to_suspense",
            "test_a_broken_identity_is_a_data_problem_balanced_to_suspense",
            "test_an_imbalance_with_the_identities_holding_fails_the_event_type",
            "test_the_circuit_breaker_trips_above_half_a_percent",
            at_least=6,
        ),
    ),
    (
        "FX, periods and corrections: each line records its rate and the event rounds once (a missing rate fails "
        "the run); a closed period posts to the first open one as a prior-period adjustment; a changed event is "
        "reversed and rebooked, a rerun books nothing, a vanished event is voided; the generator's FX rates cover "
        "every date it emits",
        lambda _: _combine(
            _tests(
                "reference",
                "test_fx_conversion_records_its_rate_and_rounds_per_event",
                "test_a_closed_period_posts_to_the_first_open_one_as_a_prior_period_adjustment",
                "test_corrections_reruns_and_voids",
                at_least=3,
            ),
            _tests("unit", "test_fx_rates_cover_every_date_the_generator_emits"),
        ),
    ),
    (
        "Determinism: shuffled input books identically, rollups sum their lines and every line links to one, and "
        "the sample books on tiny seed 11 match their golden digest",
        lambda _: _tests(
            "reference",
            "test_input_order_does_not_change_the_books",
            "test_rollups_sum_their_lines_and_every_line_links_to_one",
            "test_the_sample_books_match_the_golden_digest",
            at_least=3,
        ),
    ),
    (
        "Configuration: every sample validates, definitions typecheck against their event types and references, "
        "and the published sample request bodies match the code; through the API every sample goes from draft to "
        "active, activation waits for active references, one chart, dimension set and policy are active at a time, "
        "and a chart version cannot retire an account in use",
        lambda _: _combine(
            _tests("unit", "test_journal_config", at_least=6),
            _tests("unit", "test_published_samples_match_the_code"),
            _tests("app", "test_journaling_configs_api", at_least=10),
        ),
    ),
    (
        "Active rules stay consistent: nothing an active configuration uses can be retired (journaling kinds, and "
        "data sources an active match set uses), and a new version of an event type, mapping or dimension set "
        "cannot break an active definition",
        lambda _: _tests(
            "app",
            "test_nothing_an_active_configuration_uses_can_be_retired",
            "test_a_new_version_cannot_break_an_active_definition",
            "test_a_data_source_an_active_match_set_uses_cannot_be_retired",
            at_least=3,
        ),
    ),
    (
        "Governance covers the new kinds: the authorization matrix, the route inventory and idempotency include "
        "every journaling operation; migration 0003 lists every kind, and its downgrade and upgrade are clean",
        lambda _: _combine(
            _tests("app", "test_authorization_matrix", "test_the_matrix_covers_every_operation", at_least=2),
            _tests("app", "test_route_inventory", at_least=5),
            _tests("app", "test_idempotency_all", at_least=3),
            _tests(
                "app",
                "test_the_migrations_list_every_role_and_configuration_kind",
                "test_a_downgrade_removes_the_newer_kinds",
                at_least=2,
            ),
        ),
    ),
    (
        "Through the stack: a journaling configuration goes from draft to active as the dev users, and the stack's "
        "audit chain verifies after the whole suite",
        lambda _: _tests(
            "e2e",
            "test_journaling_configuration_goes_live_through_the_stack",
            "test_the_audit_chain_verifies_after_the_whole_suite",
            at_least=2,
        ),
    ),
    ("JSON Schema snapshots: the 5 journaling models frozen, 0 drift", _journaling_schemas),
    ("ADRs written (0001-0015)", _adrs_6ref),
]


def _gate_6ref() -> Gate:
    regression = [
        (name if name.startswith("Regression (") else f"Regression (4): {name}", check)
        for name, check in GATES["4"].criteria
        if not name.startswith("ADRs written")
    ]
    return Gate(
        number=_JOURNAL,
        title="Stage 6 first slice: journaling rules as configuration, the reference journaling engine and its "
        "oracle, on sample inputs",
        steps=_6ref_steps(),
        criteria=[*_6REF_CRITERIA, *regression],
    )


GATES[_JOURNAL] = _gate_6ref()


# ---------------------------------------------------------------- Stage 7a: journals in the control plane


_WORKFLOW_SCHEMAS = ("entity_set", "journal_template", "approval_matrix", "recurring_journal", "journal_draft")


def _workflow_schemas(r: dict[str, StepResult]) -> tuple[bool, str]:
    folder = REPO / "libs" / "contracts" / "src" / "close_contracts" / "schemas"
    missing = [n for n in _WORKFLOW_SCHEMAS if not (folder / f"{n}.schema.json").is_file()]
    ok, detail = _schemas(r)
    found = f"; missing: {', '.join(missing)}" if missing else "; 5 Stage 7 models frozen"
    return ok and not missing, detail + found


def _adrs_7a(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 17)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(
        names
    ), f"{len(present)}/{len(names)} ADRs present (0001-0016, incl. the journal workflow)"


_7A_CRITERIA: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = [
    (
        "States: every journal state against every move (a 409 for each illegal one), edits of drafts only, the "
        "current ETag on every move; batch and period moves the same way",
        lambda _: _tests(
            "app",
            "test_every_move_from_every_state",
            "test_moves_need_the_current_etag",
            "test_the_moves_and_who_may_make_them",
            "test_every_batch_move_from_every_state",
            "test_reject_recall_and_cancel",
            at_least=5,
        ),
    ),
    (
        "Separation of duties: the preparer can't approve or reject, one person approves one level (in a batch too), "
        "a delegate is checked like the person they stand in for, nobody delegates to themselves; every refusal is "
        "audited",
        lambda _: _tests(
            "app",
            "test_separation_of_duties",
            "test_delegation",
            "test_separation_of_duties_covers_every_member",
            at_least=3,
        ),
    ),
    (
        "Routing: amount thresholds, the AS 2401.61 risk score and routing rules give the expected levels; routing is "
        "frozen at submit, so a new matrix version leaves journals in approval unchanged; approvals are bound to the "
        "journal's content",
        lambda _: _combine(
            _tests("app", "test_routing_is_frozen_at_submit", "test_the_risk_score_raises_the_levels", at_least=2),
            _tests(
                "unit",
                "test_routing_takes_the_most_any_input_asks_for",
                "test_each_risk_factor",
                "test_bypass_needs_the_kind_allowed_and_a_cap",
                at_least=3,
            ),
        ),
    ),
    (
        "Periods: no submission or approval into a closed or locked period, closing is refused while journals wait, "
        "and the two-thread race test holds with the row locks and fails without them (positive control)",
        lambda _: _tests(
            "app",
            "test_closed_and_locked_periods_refuse_journals",
            "test_the_period_lock_keeps_a_journal_out_of_a_closing_period",
            "test_without_the_locks_the_race_leaves_a_journal_waiting_in_a_closed_period",
            at_least=3,
        ),
    ),
    (
        "Batches, recurring journals, reversals and reminders: a batch is approved all or none; recurring journals "
        "are created once per period at any as-of time, bypasses flagged and skips reported; reversals and automatic "
        "reversals land in the next period; reminders and escalation fire at the matrix's intervals",
        lambda _: _tests(
            "app",
            "test_a_batch_is_approved_all_or_none",
            "test_recurring_journals_are_created_once_per_period",
            "test_reversals",
            "test_an_accrual_reverses_itself_in_the_next_period",
            "test_reminders_and_escalation",
            "test_a_waiting_batch_is_reminded_too",
            at_least=6,
        ),
    ),
    (
        "Validation, import and attachments: chart, dimension, template and balance checks with stable codes; a CSV "
        "import is all or nothing with row-numbered issues; attachments keep their cap, allow-list and download "
        "headers, and a template can require one",
        lambda _: _combine(
            _tests(
                "app",
                "test_invalid_journals_are_problems_with_issues",
                "test_a_valid_file_creates_every_journal",
                "test_a_bad_file_creates_nothing_and_says_where",
                "test_attachments",
                at_least=4,
            ),
            _tests(
                "unit",
                "test_journal_validation_codes",
                "test_accounts_must_suit_the_entity_and_its_currency",
                "test_column_rules",
                at_least=3,
            ),
        ),
    ),
    (
        "Completeness: numbers are gapless per entity (a refused journal takes none), the population export and its "
        "summary tie to the journal table and neutralise spreadsheet formulas, the exception report lists what was "
        "rejected, recalled, cancelled or bypassed, and the app role can't delete or rewrite any of it",
        lambda _: _tests(
            "app",
            "test_numbers_are_gapless_and_the_population_ties_out",
            "test_the_export_neutralises_spreadsheet_formulas",
            "test_up_down_up_is_clean_and_keeps_the_privilege_rules",
            at_least=3,
        ),
    ),
    (
        "Configuration: entities, templates, the approval matrix and recurring journals go through maker-checker with "
        "their activation, singleton and retirement rules; every published sample goes from draft to active",
        lambda _: _combine(
            _tests("app", "test_workflow_configs_api", at_least=7),
            _tests("app", "test_every_sample_goes_from_draft_to_active"),
            _tests("unit", "test_journal_workflow", at_least=10),
        ),
    ),
    (
        "Governance covers every new operation: the authorization matrix (entity-scoped journal rows), the route "
        "inventory and idempotency with its audit counts; migration 0004 lists every kind and its downgrade is clean",
        lambda _: _combine(
            _tests("app", "test_authorization_matrix", "test_the_matrix_covers_every_operation", at_least=2),
            _tests("app", "test_route_inventory", at_least=5),
            _tests("app", "test_every_post_replays_byte_identically_without_a_second_effect"),
            _tests(
                "app",
                "test_the_migrations_list_every_role_and_configuration_kind",
                "test_a_downgrade_removes_the_newer_kinds",
                at_least=3,
            ),
        ),
    ),
    (
        "Through the stack: alice prepares a journal, dave approves level 1 and erin level 2, SoD refusals hold, "
        "another entity's user can't see it, and the stack's audit chain verifies after the whole suite",
        lambda _: _tests(
            "e2e",
            "test_a_journal_goes_through_two_approval_levels_on_the_stack",
            "test_the_audit_chain_verifies_after_the_whole_suite",
            at_least=2,
        ),
    ),
    ("JSON Schema snapshots: 0 drift, with the 5 Stage 7 models frozen", _workflow_schemas),
    ("ADRs written (0001-0016)", _adrs_7a),
]


def _gate_7a() -> Gate:
    regression = [
        (name if name.startswith("Regression (") else f"Regression (6-ref): {name}", check)
        for name, check in GATES[_JOURNAL].criteria
        if not name.startswith("ADRs written")
    ]
    return Gate(
        number="7a",
        title="Stage 7a: journals in the control plane (templates, approvals, periods, batches, recurring journals, "
        "evidence), on sample inputs",
        steps=_6ref_steps("7a"),
        criteria=[*_7A_CRITERIA, *regression],
    )


GATES["7a"] = _gate_7a()


# ---------------------------------------------------------------- Stage 7b: posting to the Databricks ledger


def _ledger_schemas(r: dict[str, StepResult]) -> tuple[bool, str]:
    registry = REPO / "libs" / "contracts" / "src" / "close_contracts" / "schemas" / "table_registry.schema.json"
    text = registry.read_text(encoding="utf-8") if registry.is_file() else ""
    missing = [t for t in ("journal_lines", "journal_postings") if t not in text]
    ok, detail = _schemas(r)
    found = f"; missing from the table registry: {', '.join(missing)}" if missing else "; the 2 ledger tables frozen"
    return ok and not missing, detail + found


def _adrs_7b(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 18)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(names), f"{len(present)}/{len(names)} ADRs present (0001-0017, incl. posting)"


_7B_CRITERIA: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = [
    (
        "States: posting and the write-back from every journal state, legal and illegal, as a table; the 7a moves "
        "on posting, posted and confirmed journals are refused except reversing what is in the ledger",
        lambda _: _tests("app", "test_every_posting_move_from_every_state"),
    ),
    (
        "Exactly once: a run posts each journal once; a job killed after its ledger commit leaves the journals "
        "approved, and the retry finds the lines, adds none, completes the ERP feed and ends them posted; a stale "
        "writer is fenced before it writes; batches post whole, even when a run fills up",
        lambda _: _combine(
            _tests(
                "app",
                "test_a_run_posts_the_approved_journals_and_the_ledger_ties_out",
                "test_a_failed_run_returns_its_journals_and_a_retry_finds_what_a_lost_result_hid",
                "test_batches_post_whole",
                at_least=3,
            ),
            _tests(
                "spark",
                "test_each_line_is_written_once_however_often_it_is_posted",
                "test_a_crash_between_the_two_commits_is_finished_by_the_rerun",
                "test_a_stale_writer_is_fenced_before_it_writes",
                at_least=3,
            ),
            _tests("e2e", "test_a_job_killed_after_its_ledger_commit_posts_once_on_the_retry"),
        ),
    ),
    (
        "Separation of duties and periods: the requester's own journals (and batches holding one) are skipped; a "
        "period won't close while journals are approved but unposted, or posting; a close racing a posting start "
        "is refused whichever commits first, with the locks (the close waits) and without them (the state check)",
        lambda _: _tests(
            "app",
            "test_the_requester_never_posts_their_own_journals",
            "test_periods_wait_for_posting_and_posted_journals_reverse",
            "test_closed_and_locked_periods_refuse_journals",
            "test_a_close_racing_a_posting_start_is_refused_either_way",
            at_least=5,
        ),
    ),
    (
        "Posted journals keep the 7a rules: an automatic reversal is still created when the next period opens, and "
        "a posted journal still counts as using its accounts in the risk score",
        lambda _: _tests(
            "app",
            "test_a_posted_accrual_still_reverses_when_the_next_period_opens",
            "test_a_posted_journal_still_counts_as_using_its_accounts",
            at_least=2,
        ),
    ),
    (
        "The ERP round trip: the stub ERP reads the feed, books balanced journals and writes document numbers back; "
        "the journals become erp_confirmed; the same number again is a no-op, a different one a 409, a journal "
        "outside the client's entities a 404",
        lambda _: _combine(
            _tests("app", "test_the_erp_write_back"),
            _tests("unit", "test_the_stub_books_balanced_journals_and_writes_their_numbers_back"),
            _tests("e2e", "test_a_journal_goes_from_draft_through_the_ledger_to_the_erp_on_the_stack"),
        ),
    ),
    (
        "Trial balance and drill-down agree with the posted journals and never cross entities; the ledger tables "
        "hold the columns and unique keys the read path serves",
        lambda _: _combine(
            _tests(
                "app",
                "test_a_run_posts_the_approved_journals_and_the_ledger_ties_out",
                "test_the_ledger_reads_never_cross_entities",
                at_least=2,
            ),
            _tests("spark", "test_each_line_is_written_once_however_often_it_is_posted"),
            _tests("unit", "test_registry_holds_only_the_allowlisted_tables"),
        ),
    ),
    (
        "Completeness: the Postgres-to-Delta tie-out holds after every posting (including on the stack after the "
        "killed job), and a difference is reported, never smoothed over",
        lambda _: _combine(
            _tests(
                "app",
                "test_a_run_posts_the_approved_journals_and_the_ledger_ties_out",
                "test_a_difference_between_postgres_and_the_ledger_is_reported",
                at_least=2,
            ),
            _tests("e2e", "test_14_posting", at_least=2),
        ),
    ),
    (
        "Least privilege: the ledger reader can select only the posting queue (in Postgres and from inside the "
        "jobs container); its password reaches only the jobs service and the deploy step that creates the role",
        lambda _: _combine(
            _tests("app", "test_the_ledger_reader_role_sees_only_the_posting_queue"),
            _tests(
                "e2e",
                "test_the_ledger_reader_can_read_only_",  # its views: the posting queue, and from 8a the snapshots
                "test_each_database_password_reaches_only_its_role_and_postgres",
                at_least=2,
            ),
        ),
    ),
    (
        "XLSX: a workbook imports exactly like its CSV and is recorded under its own hash; zip bombs, files that "
        "aren't workbooks, formulas without calculated values and Excel's float noise are refused; one workbook is "
        "parsed at a time, outside any transaction",
        lambda _: _tests(
            "app",
            "test_a_workbook_imports_like_its_csv",
            "test_what_a_workbook_cannot_do",
            "test_one_workbook_is_read_at_a_time",
            at_least=3,
        ),
    ),
    (
        "Governance covers every new operation: the authorization matrix (posting and ledger rows), the route "
        "inventory and idempotency with its audit counts; migration 0005 runs up, down and up with its privileges",
        lambda _: _combine(
            _tests("app", "test_authorization_matrix", "test_the_matrix_covers_every_operation", at_least=2),
            _tests("app", "test_route_inventory", at_least=5),
            _tests("app", "test_every_post_replays_byte_identically_without_a_second_effect"),
            _tests("app", "test_up_down_up_is_clean_and_keeps_the_privilege_rules"),
        ),
    ),
    (
        "Through the stack: alice prepares, dave and erin approve, dave posts, the job writes the ledger, the trial "
        "balance shows the journal and the stub ERP confirms it; the audit chain verifies after the whole suite",
        lambda _: _tests(
            "e2e",
            "test_a_journal_goes_from_draft_through_the_ledger_to_the_erp_on_the_stack",
            "test_the_audit_chain_verifies_after_the_whole_suite",
            at_least=2,
        ),
    ),
    (
        "Migration and contracts: DDL V007 applies through ddl_apply (and a rerun applies nothing), every "
        "registered table matches its contract, and the posting job uses the run's clock, never the wall clock",
        lambda _: _combine(
            _tests(
                "spark",
                "test_every_registered_table_matches_its_contract",
                "test_ddl_apply_is_fast_and_a_rerun_applies_nothing",
                "test_migration_versions_are_contiguous_and_checksummed",
                at_least=3,
            ),
            _tests("unit", "test_cli_fails_on_violations_and_passes_on_engine_src"),
        ),
    ),
    ("JSON Schema snapshots: 0 drift, with the ledger tables in the table registry", _ledger_schemas),
    ("ADRs written (0001-0017)", _adrs_7b),
]


def _gate_7b() -> Gate:
    regression = [
        (name if name.startswith("Regression (") else f"Regression (7a): {name}", check)
        for name, check in GATES["7a"].criteria
        if not name.startswith("ADRs written")
    ]
    return Gate(
        number="7b",
        title="Stage 7b: posting to the Databricks ledger, the ERP write-back, trial balance and XLSX import, on "
        "sample inputs",
        steps=_6ref_steps("7b"),
        criteria=[*_7B_CRITERIA, *regression],
    )


GATES["7b"] = _gate_7b()


# ---------------------------------------------------------------- Stage 7c: multi-currency manual journals


def _fx_schemas(r: dict[str, StepResult]) -> tuple[bool, str]:
    folder = REPO / "libs" / "contracts" / "src" / "close_contracts" / "schemas"
    expected = {
        "journal_draft.schema.json": '"currency"',
        "journal_template.schema.json": '"currencies"',
        "recurring_journal.schema.json": '"currency"',
        "table_registry.schema.json": '"fx_rate"',
    }
    missing = [name for name, needle in expected.items() if needle not in (folder / name).read_text(encoding="utf-8")]
    ok, detail = _schemas(r)
    found = f"; not yet in: {', '.join(missing)}" if missing else "; the 4 changed snapshots carry the currency fields"
    return ok and not missing, detail + found


def _parity_19(r: dict[str, StepResult]) -> tuple[bool, str]:
    runs = re.findall(r"(\d+) passed", r["S2 parity x3 on SeaweedFS"].output)
    ok = len(runs) == 3 and set(runs) == {"19"}
    return ok, f"parity runs {runs}: ADD COLUMNS on an append-only table is feature 19 (ADR-0002)"


def _adrs_7c(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 19)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(names), f"{len(present)}/{len(names)} ADRs present (0001-0018, incl. multi-currency)"


_7C_CRITERIA: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = [
    (
        "D9 conversion: over 10,000 generated journals in currencies with 0, 2 and 3 decimals, every line equals "
        "convert_minor and the functional debits equal the credits with one rounding line of at most half a minor "
        "unit per line; a submitted EUR journal freezes its rate, its lines and the rounding line",
        lambda _: _combine(
            _tests(
                "unit",
                "test_every_conversion_balances_within_half_a_minor_unit_per_line",
                "test_a_conversion_rounds_each_line_and_balances_on_one_rounding_line",
                at_least=2,
            ),
            _tests("app", "test_a_foreign_currency_journal_is_converted_at_submit_and_bound_into_its_approvals"),
        ),
    ),
    (
        "Frozen and bound: a rate published later never changes a submitted journal, a recall and resubmit "
        "converts again, every approval records the conversion hash, a changed conversion row is caught when "
        "approving, and a missing rate refuses the submit",
        lambda _: _tests(
            "app",
            "test_a_rate_published_later_never_changes_a_submitted_journal",
            "test_a_changed_conversion_is_caught_when_approving",
            "test_a_missing_rate_refuses_the_submit",
            at_least=3,
        ),
    ),
    (
        "Functional terms: routing and bypass caps use the functional total; a reversal that is still the plain "
        "flip of its original reuses the original's rate and nets to zero on every account, an edited one converts "
        "afresh, and an automatic reversal bypasses at the original's rate",
        lambda _: _combine(
            _tests(
                "app",
                "test_routing_uses_the_functional_total",
                "test_reversals_reuse_the_original_rate_and_net_to_zero",
                "test_an_automatic_reversal_bypasses_at_the_original_rate",
                at_least=3,
            ),
            _tests(
                "unit",
                "test_routing_uses_the_functional_total_it_is_given",
                "test_only_an_unedited_reversal_reuses_the_original_rate",
                at_least=2,
            ),
        ),
    ),
    (
        "Narrow rates: typed keys, the latest version read at one pinned Delta version, nothing for a missing key "
        "or table; ref.fx_rates is not queryable through specs; every authorization, ETag and state check comes "
        "before the lookup, which holds no lock (another transaction can lock the journal while it waits), and a "
        "duplicate submit that lost the race to the first still replays",
        lambda _: _combine(
            _tests("unit", "test_a_key_is_typed", "test_rates_are_exact_text_and_the_latest_version_wins", at_least=2),
            _tests("unit", "test_registry_holds_only_the_allowlisted_tables"),
            _tests(
                "platform",
                "test_the_latest_version_at_one_pinned_table_version",
                "test_no_table_means_no_rates",
                at_least=2,
            ),
            _tests(
                "app",
                "test_every_check_comes_before_the_rate_lookup",
                "test_the_lookup_holds_no_lock",
                "test_a_duplicate_submit_that_lost_the_race_replays",
                at_least=3,
            ),
        ),
    ),
    (
        "Batches and recurring journals: every member is converted at the batch's submit and one missing rate "
        "refuses the batch; an auto-approved recurring journal is skipped (and its preparer told) until its rate "
        "is published, then converted as it bypasses approval",
        lambda _: _tests(
            "app",
            "test_a_batch_converts_every_member_and_one_missing_rate_refuses_it",
            "test_an_auto_approved_recurring_journal_converts_when_its_rate_is_published",
            at_least=2,
        ),
    ),
    (
        "Posting: ledger lines carry both amounts, the rate with its date, type and version, and the rounding line "
        "(line_kind fx_rounding); debits and credits are functional, the trial balance balances and the tie-out "
        "holds; the ERP feed carries the conversion",
        lambda _: _combine(
            _tests("app", "test_posting_writes_functional_amounts_the_rate_and_the_rounding_line"),
            _tests("spark", "test_a_foreign_currency_journal_posts_functional_amounts_its_rate_and_a_rounding_line"),
            _tests("unit", "test_a_foreign_currency_journal_balances_in_both_currencies"),
        ),
    ),
    (
        "Import, evidence and the rate route: a currency column reads each currency's decimals (none for JPY, "
        "three for KWD) and refuses extra ones; the population shows the currency, rate and rounding; "
        "GET /fx-rates answers journal readers and refuses bad requests",
        lambda _: _tests(
            "app",
            "test_an_import_reads_each_currency_with_its_own_decimals",
            "test_the_population_shows_the_conversion",
            "test_the_rate_route",
            at_least=3,
        ),
    ),
    (
        "Compatibility: a functional-currency draft hashes exactly as before Stage 7c, the upgrade backfills every "
        "live journal and a journal pending across it still approves, and the downgrade refuses while conversions "
        "exist",
        lambda _: _combine(
            _tests("unit", "test_a_functional_currency_draft_hashes_as_before_stage_7c"),
            _tests(
                "app",
                "test_the_upgrade_backfills_live_journals_and_they_still_approve",
                "test_the_downgrade_refuses_while_conversions_exist",
                at_least=2,
            ),
        ),
    ),
    (
        "Ledger schema: DDL V008 evolves ledger.journal_lines with ADD COLUMNS (parity feature 19, 3 of 3 runs on "
        "the S3-compatible store), every registered table matches its contract, and the evolved table still serves "
        "the read path",
        lambda r: _combine(
            _parity_19(r),
            _tests(
                "spark",
                "test_every_registered_table_matches_its_contract",
                "test_migration_versions_are_contiguous_and_checksummed",
                at_least=2,
            ),
        ),
    ),
    (
        "Governance covers every new operation: the authorization matrix (the rate route), the route inventory and "
        "idempotency; migration 0006 runs up, down and up with the conversion table insert-only",
        lambda _: _combine(
            _tests("app", "test_authorization_matrix", "test_the_matrix_covers_every_operation", at_least=2),
            _tests("app", "test_route_inventory", at_least=5),
            _tests("app", "test_every_post_replays_byte_identically_without_a_second_effect"),
            _tests("app", "test_up_down_up_is_clean_and_keeps_the_privilege_rules"),
        ),
    ),
    (
        "Through the stack: the generator's rates are in the local lake; alice prepares a EUR journal in US01 at "
        "the rate the route shows, dave and erin approve, dave posts; its ledger lines are USD under D9, the trial "
        "balance balances, the tie-out holds and the stub ERP confirms it; the audit chain verifies",
        lambda _: _tests(
            "e2e",
            "test_a_euro_journal_is_converted_posted_and_confirmed_on_the_stack",
            "test_the_audit_chain_verifies_after_the_whole_suite",
            at_least=2,
        ),
    ),
    (
        "JSON Schema snapshots: 0 drift, with the journal currency, template currencies and ledger rate columns",
        _fx_schemas,
    ),
    ("ADRs written (0001-0018)", _adrs_7c),
]


def _gate_7c() -> Gate:
    regression = [
        (name if name.startswith("Regression (") else f"Regression (7b): {name}", check)
        for name, check in GATES["7b"].criteria
        if not name.startswith("ADRs written")
    ]
    return Gate(
        number="7c",
        title="Stage 7c: multi-currency manual journals (D9 at submit, the narrow rate lookup), on sample inputs",
        steps=_6ref_steps("7c"),
        criteria=[*_7C_CRITERIA, *regression],
    )


GATES["7c"] = _gate_7c()


# ---------------------------------------------------------------- Stage 8a: the general ledger


def _rollup_schemas(r: dict[str, StepResult]) -> tuple[bool, str]:
    registry = REPO / "libs" / "contracts" / "src" / "close_contracts" / "schemas" / "table_registry.schema.json"
    text = registry.read_text(encoding="utf-8")
    missing = [c for c in ("functional_debit_minor", "functional_credit_minor") if c not in text]
    ok, detail = _schemas(r)
    found = f"; not yet in the table registry: {', '.join(missing)}" if missing else "; the rollups' functional amounts"
    return ok and not missing, detail + found


def _adrs_8a(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 20)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(names), f"{len(present)}/{len(names)} ADRs present (0001-0019, incl. the ledger)"


_8A_CRITERIA: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = [
    (
        "The stand-in equals the engine through Delta: the generator's tiny rows (seed 11) journaled for US01 equal "
        "the reference engine run in memory, line for line and rollup for rollup (decimals, dates, maps and nulls "
        "after the round trip), and the rollups sum to their lines",
        lambda _: _tests("spark", "test_the_stand_in_writes_what_the_engine_books_in_memory"),
    ),
    (
        "Reruns: a rerun writes nothing; more landing data and a changed configuration are refused, by the pins "
        "and, for a ledger no run has pinned, by comparing what is already written; a run waits for the previous "
        "one to be settled, so it always sees the pin",
        lambda _: _combine(
            _tests(
                "spark",
                "test_a_rerun_writes_nothing_and_anything_changed_is_refused",
                "test_rows_an_unpinned_run_would_not_write_as_they_are_are_refused",
                at_least=2,
            ),
            _tests(
                "app",
                "test_a_run_snapshots_what_it_depends_on_and_its_first_success_pins_the_ledger",
                "test_a_changed_configuration_is_refused_and_refusals_and_failures_are_recorded",
                "test_a_run_waits_for_the_previous_one_to_be_settled_not_only_for_the_lease",
                at_least=3,
            ),
        ),
    ),
    (
        "Snapshots and periods: a run uses the configuration, entity set and period statuses frozen by hash when it "
        "started, and a changed snapshot is refused; snapshots are insert-only; the reader role sees only snapshots "
        "of runs in progress; an event dated in a closed period lands in the first open one; a period can't close "
        "while a run is in progress",
        lambda _: _combine(
            _tests(
                "app",
                "test_a_run_snapshots_what_it_depends_on_and_its_first_success_pins_the_ledger",
                "test_the_reader_role_sees_only_snapshots_of_runs_in_progress",
                "test_a_period_cannot_close_while_a_run_is_in_progress",
                "test_up_down_up_is_clean_and_keeps_the_privilege_rules",
                at_least=4,
            ),
            _tests(
                "spark",
                "test_an_event_in_a_closed_period_lands_in_the_first_open_one",
                "test_a_rerun_writes_nothing_and_anything_changed_is_refused",
                at_least=2,
            ),
        ),
    ),
    (
        "Balances: over both sources (the engine's rollups and the posted manual journals), opening plus activity "
        "equals closing for every account and period, each period opens where the last closed (a break is "
        "reported), debits equal credits, and a rollup without functional amounts fails loudly",
        lambda _: _tests(
            "app",
            "test_balances_roll_forward_and_trial_balance_read_both_sources",
            "test_the_roll_forward_reports_a_break_and_a_rollup_without_functional_amounts_fails",
            at_least=2,
        ),
    ),
    (
        "The ERP tie-out: a matching extract ties; an ERP-only entry is reported against its account; an account "
        "only one side has, a sign error and an extract for the wrong period are each caught; a bad extract creates "
        "nothing, names its row and is kept; balances are read exactly in each currency's decimals",
        lambda _: _combine(
            _tests(
                "app",
                "test_an_erp_extract_ties_and_every_difference_is_reported",
                "test_a_bad_extract_is_refused_with_its_rows_and_kept",
                "test_the_extract_parser_reads_each_currency_exactly_and_refuses_what_it_cannot_read",
                at_least=3,
            ),
            _tests("unit", "test_the_stub_trial_balance_is_the_gl_plus_erp_only_entries"),
        ),
    ),
    (
        "Exactly once: a run that dies between its line and rollup commits is completed by the retry, each line "
        "written once; a fence check precedes every commit, so a writer fenced after its line commit writes no "
        "rollups (and a posting run no feed); the ledger lease keeps journaling and posting apart",
        lambda _: _combine(
            _tests(
                "spark",
                "test_a_run_that_dies_between_lines_and_rollups_is_completed_by_the_retry",
                "test_a_crash_between_the_two_commits_is_finished_by_the_rerun",
                "test_a_stale_writer_is_fenced_before_it_writes",
                "test_a_writer_fenced_between_its_commits_writes_no_rollups",
                "test_a_writer_fenced_between_its_commits_writes_no_feed",
                at_least=5,
            ),
            _tests("app", "test_the_ledger_lease_keeps_journaling_and_posting_apart"),
        ),
    ),
    (
        "Governance: the authorization matrix, route inventory and idempotency cover every new operation; the "
        "reader role is checked in Postgres and from inside the jobs container; migration 0007 runs up, down and "
        "up; the audit chain verifies",
        lambda _: _combine(
            _tests("app", "test_authorization_matrix", "test_the_matrix_covers_every_operation", at_least=2),
            _tests("app", "test_route_inventory", at_least=5),
            _tests("app", "test_every_post_replays_byte_identically_without_a_second_effect"),
            _tests(
                "app",
                "test_up_down_up_is_clean_and_keeps_the_privilege_rules",
                "test_the_reader_role_sees_only_snapshots_of_runs_in_progress",
                at_least=2,
            ),
            _tests(
                "e2e",
                "test_the_ledger_reader_can_read_only_its_views",
                "test_the_audit_chain_verifies_after_the_whole_suite",
                at_least=2,
            ),
        ),
    ),
    (
        "Through the stack: the generator's tiny data is journaled for US01 and a rerun writes nothing; the "
        "balances balance and the roll-forward ties; the stub ERP's extract is uploaded and the tie-out reports "
        "exactly its ERP-only entry; the audit chain verifies",
        lambda _: _tests("e2e", "test_the_general_ledger_is_journaled_balanced_and_tied_to_the_erp_on_the_stack"),
    ),
    (
        "Contracts: the JSON Schema snapshots show 0 drift after the deliberate regeneration, with the rollups' "
        "functional amounts",
        _rollup_schemas,
    ),
    ("ADRs written (0001-0019)", _adrs_8a),
]


def _gate_8a() -> Gate:
    regression = [
        (name if name.startswith("Regression (") else f"Regression (7c): {name}", check)
        for name, check in GATES["7c"].criteria
        if not name.startswith("ADRs written")
    ]
    return Gate(
        number="8a",
        title="Stage 8a: the general ledger (the journaling stand-in, balances, the ERP tie-out), on sample inputs",
        steps=_6ref_steps("8a"),
        criteria=[*_8A_CRITERIA, *regression],
    )


GATES["8a"] = _gate_8a()


# ---------------------------------------------------------------- Stage 8b: the close


def _close_schemas(r: dict[str, StepResult]) -> tuple[bool, str]:
    folder = REPO / "libs" / "contracts" / "src" / "close_contracts" / "schemas"
    missing = [n for n in ("close_checklist", "flux_rules") if not (folder / f"{n}.schema.json").is_file()]
    ok, detail = _schemas(r)
    found = f"; missing: {', '.join(missing)}" if missing else "; the close checklist and flux rules frozen"
    return ok and not missing, detail + found


def _adrs_8b(_: dict[str, StepResult]) -> tuple[bool, str]:
    names = [f"{n:04d}-" for n in range(1, 21)]
    present = [n for n in names if list((REPO / "docs/adr").glob(f"{n}*.md"))]
    return len(present) == len(names), f"{len(present)}/{len(names)} ADRs present (0001-0020, incl. the close)"


_8B_CRITERIA: list[tuple[str, Callable[[dict[str, StepResult]], tuple[bool, str]]]] = [
    (
        "The soft close: a soft-closed period takes manual entries (journals created, edited, approved, imported "
        "and posted) and refuses automated ones (recurring journals, automatic reversals, the journaling engine, "
        "which books its events in the first open period); a soft close waits for a journaling run",
        lambda _: _combine(
            _tests(
                "app",
                "test_a_soft_closed_period_takes_manual_entries",
                "test_a_soft_closed_period_refuses_automated_entries",
                "test_a_soft_close_waits_for_a_journaling_run",
                at_least=3,
            ),
            _tests("spark", "test_an_event_in_a_closed_period_lands_in_the_first_open_one"),
        ),
    ),
    (
        "The checklist's configuration: duplicate ids, unknown dependencies, cycles, a reviewer who is the "
        "preparer, a manual task without a reviewer, and a dependency on a narrower task are refused; on "
        "activation a preparer or reviewer without the role, and an entity outside the set, are refused",
        lambda _: _combine(
            _tests(
                "unit",
                "test_a_task_is_manual_or_automatic_with_distinct_people",
                "test_a_checklist_has_unique_tasks_known_dependencies_and_no_cycle",
                at_least=15,
            ),
            _tests("app", "test_a_checklist_needs_people_with_the_roles_and_entities_in_the_set"),
        ),
    ),
    (
        "Due dates: test vectors pin BD+n and BD-n, a month ending on a weekend and one ending on a holiday; "
        "generation reads the entity's calendar through the narrow lookup (one pinned version, nothing without a "
        "calendar code or the tables), freezes the tasks, and is refused when the calendar can't give the dates",
        lambda _: _combine(
            _tests(
                "unit",
                "test_due_dates_count_business_days_from_the_period_end",
                "test_a_month_that_ends_on_a_holiday",
                "test_a_calendar_that_stops_short_gives_no_date",
                at_least=12,
            ),
            _tests(
                "platform",
                "test_an_entity_gets_its_calendar_days_at_one_pinned_version",
                "test_no_tables_means_no_calendar",
                at_least=2,
            ),
            _tests(
                "app",
                "test_a_checklist_is_generated_with_business_day_due_dates_and_frozen",
                "test_generating_is_refused_without_a_calendar_or_a_checklist",
                at_least=2,
            ),
        ),
    ),
    (
        "Tasks: dependencies are enforced, only the assignee acts, a reviewer never reviews their own "
        "preparation, a send-back, a reopen and a reassignment are kept and audited, and an automatic task records "
        "its check and evidence",
        lambda _: _tests(
            "app",
            "test_tasks_follow_their_dependencies_assignees_and_reviewers",
            "test_an_automatic_task_records_its_check_and_evidence",
            at_least=2,
        ),
    ),
    (
        "Reminders: once a day from the due date, to the preparer or the reviewer the task waits on, and to the "
        "controller after the checklist's days overdue",
        lambda _: _tests("app", "test_reminders_go_once_a_day_to_whoever_a_task_waits_on"),
    ),
    (
        "Flux: thresholds per type, override, currency and operator, closing balances or activity by type, and a "
        "change from zero over any percent; an explanation is bound to the server's amounts and goes stale; the "
        "review needs every line explained, by others, and its hash detects a later change",
        lambda _: _combine(
            _tests(
                "unit",
                "test_a_threshold_joins_its_amount_and_percent",
                "test_flux_rules_cover_every_account_type",
                "test_a_flux_line_measures_by_account_type",
                "test_the_flux_hash_binds_lines_and_explanations",
                at_least=4,
            ),
            _tests(
                "app",
                "test_each_account_is_compared_with_the_prior_period",
                "test_an_explanation_is_bound_to_the_servers_amounts_and_goes_stale",
                "test_a_review_needs_every_line_explained_by_others_and_binds_to_them",
                at_least=3,
            ),
        ),
    ),
    (
        "The hard close: a period with a checklist is soft-closed first and closes only when every task is done, "
        "never on a GL check made before the ledger's last change; a period without one closes as before",
        lambda _: _tests(
            "app",
            "test_a_period_with_a_checklist_closes_once_every_task_is_done",
            "test_a_gl_check_made_before_the_ledger_changed_is_made_again",
            at_least=2,
        ),
    ),
    (
        "Governance: the authorization matrix, route inventory and idempotency cover every new operation; "
        "migration 0008 runs up, down and up, and its downgrade refuses while a period is soft-closed; the audit "
        "chain verifies",
        lambda _: _combine(
            _tests("app", "test_authorization_matrix", "test_the_matrix_covers_every_operation", at_least=2),
            _tests("app", "test_route_inventory", at_least=5),
            _tests("app", "test_every_post_replays_byte_identically_without_a_second_effect"),
            _tests(
                "app",
                "test_up_down_up_is_clean_and_keeps_the_privilege_rules",
                "test_the_downgrade_refuses_while_a_period_is_soft_closed",
                at_least=2,
            ),
            _tests("e2e", "test_the_audit_chain_verifies_after_the_whole_suite"),
        ),
    ),
    (
        "Through the stack: dave generates US01's July checklist from the sample (due dates from US_FED in the "
        "lake) and soft-closes July; alice explains the flux and erin reviews it; the manual tasks are prepared "
        "and reviewed and the automatic checks pass; dave closes July",
        lambda _: _tests("e2e", "test_a_period_is_soft_closed_reviewed_and_closed_on_the_stack"),
    ),
    (
        "Contracts: the JSON Schema snapshots show 0 drift after the deliberate regeneration, with the close "
        "checklist and the flux rules",
        _close_schemas,
    ),
    ("ADRs written (0001-0020)", _adrs_8b),
]


def _gate_8b() -> Gate:
    regression = [
        (name if name.startswith("Regression (") else f"Regression (8a): {name}", check)
        for name, check in GATES["8a"].criteria
        if not name.startswith("ADRs written")
    ]
    return Gate(
        number="8b",
        title="Stage 8b: the close (the soft close, the close checklist, flux analysis), on sample inputs",
        steps=_6ref_steps("8b"),
        criteria=[*_8B_CRITERIA, *regression],
    )


GATES["8b"] = _gate_8b()


def _environment() -> list[tuple[str, str]]:
    def sh(argv: list[str]) -> str:
        return subprocess.run(argv, capture_output=True, text=True, check=False).stdout.strip()

    return [
        ("Host", f"{platform.system()} {platform.release()} ({platform.machine()})"),
        ("CPU", sh(["sysctl", "-n", "machdep.cpu.brand_string"]) or "?"),
        ("Memory", f"{int(sh(['sysctl', '-n', 'hw.memsize']) or 0) // 2**30} GiB"),
        ("Docker VM memory", f"{int(sh(['docker', 'info', '--format', '{{.MemTotal}}']) or 0) / 2**30:.2f} GiB"),
        ("Git", sh(["git", "log", "-1", "--format=%h %s"]) or "no commits yet (nothing is committed until asked)"),
    ]


def write_report(
    gate: Gate, results: list[StepResult], verdicts: list[tuple[str, bool, str]], started: datetime
) -> Path:
    passed = all(r.ok for r in results) and all(ok for _, ok, _ in verdicts)
    out = REPO / "docs" / "stages" / f"stage-{gate.number}-report.md"
    summary = REPO / "docs" / "stages" / f"stage-{gate.number}-summary.md"
    lines = [
        f"# Stage {gate.number} gate report: {gate.title}",
        "",
        f"**Result: {'PASS' if passed else 'FAIL'}**, generated by `make gate-{gate.number}` on "
        f"{started:%Y-%m-%d %H:%M} UTC. Total time {sum(r.seconds for r in results) / 60:.1f} min.",
        "",
    ]
    if summary.exists():
        lines += [summary.read_text(encoding="utf-8").strip(), ""]
    lines += ["## Gate criteria", "", "| Criterion | Result | Evidence |", "|---|---|---|"]
    lines += [f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |" for name, ok, detail in verdicts]
    lines += ["", "## Steps", "", "| # | Step | Result | Time | Log |", "|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        lines.append(
            f"| {i} | {r.name} | {'ok' if r.ok else 'FAILED'} | {r.seconds:.1f}s | `{r.log.relative_to(REPO)}` |"
        )
    lines += ["", "## Environment", "", "| Item | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in _environment()]
    lines += ["", "## Command output (tails)", ""]
    for r in results:
        lines += [f"**{r.name}** (`$ {r.command}`)", "", "```text", _tail(r.output), "```", ""]
    text = "\n".join(line.rstrip() for line in "\n".join(lines).splitlines())
    out.write_text(text.rstrip("\n") + "\n", encoding="utf-8")  # clean for the pre-commit fixers
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=sorted(GATES))
    args = parser.parse_args(argv)
    gate = GATES[args.gate]
    CURRENT["stage"] = gate.number
    started = datetime.now(tz=UTC)
    log_dir = REPO / ".local" / "gate" / f"stage-{gate.number}"
    log_dir.mkdir(parents=True, exist_ok=True)
    results = [_run_step(name, cmd, exp, log_dir, i) for i, (name, cmd, exp) in enumerate(gate.steps, 1)]
    by_name = {r.name: r for r in results}
    verdicts = []
    for name, check in gate.criteria:
        try:
            ok, detail = check(by_name)
        except (KeyError, ValueError, AttributeError) as exc:
            ok, detail = False, f"could not evaluate: {exc}"
        verdicts.append((name, ok, detail))
        print(f"[gate] {'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)
    report = write_report(gate, results, verdicts, started)
    passed = all(r.ok for r in results) and all(ok for _, ok, _ in verdicts)
    print(f"[gate] Stage {gate.number}: {'PASS' if passed else 'FAIL'}. Report: {report.relative_to(REPO)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
