"""Strict bundle parsing: the emulator runs exactly the jobs the bundle deploys, or refuses."""

from __future__ import annotations

from pathlib import Path

import pytest

from close_jobs_emulator.bundle import BundleError, load_bundle

REPO = Path(__file__).resolve().parents[3]
BUNDLE = (REPO / "engine" / "databricks.yml").read_text()


def _load(tmp_path: Path, text: str) -> dict[int, object]:
    path = tmp_path / "databricks.yml"
    path.write_text(text)
    return load_bundle(path, job_ids={"smoke": 1001})  # type: ignore[return-value]


def test_the_real_bundle_parses_and_renders_the_contract(tmp_path: Path) -> None:
    jobs = load_bundle(REPO / "engine" / "databricks.yml", job_ids={"smoke": 1001})
    job = jobs[1001]
    assert job.key == "smoke"
    assert job.task.entry_point == "close-job"
    assert job.timeout_seconds == 900
    args = job.render_args({"run_id": "a" * 32, "fencing_token": "7"})
    assert args[:3] == ["smoke", "--run-id", "a" * 32]
    assert args[args.index("--fencing-token") + 1] == "7"
    assert args[args.index("--root-uri") + 1] == "s3a://close-lake/local"  # ${var.lake_root} resolved


def test_unknown_job_keys_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="unsupported keys"):
        _load(tmp_path, BUNDLE.replace("max_concurrent_runs: 1", "max_concurrent_runs: 1\n      continuous: {}"))


def test_retries_must_belong_to_the_app(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="max_retries must be 0"):
        _load(tmp_path, BUNDLE.replace("max_retries: 0", "max_retries: 2"))


def test_targets_may_not_override_parameters_or_tasks(tmp_path: Path) -> None:
    override = BUNDLE + "  dev:\n    resources:\n      jobs:\n        smoke:\n          tasks: []\n"
    with pytest.raises(BundleError, match="overrides"):
        _load(tmp_path, override)


def test_task_cannot_reference_an_undeclared_parameter(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="undeclared parameter"):
        _load(tmp_path, BUNDLE.replace("{{job.parameters.rows}}", "{{job.parameters.row_count}}"))


def test_unknown_top_level_keys_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="top-level"):
        _load(tmp_path, BUNDLE + "experimental: {}\n")


def test_the_connect_server_reads_with_the_reader_identity_and_the_pinned_settings() -> None:
    from close_contracts.spark_runtime import PINNED_SPARK_CONF
    from close_jobs_emulator.connect_server import SERVER_CLASS, command

    env = {
        "CLOSE_S3_ENDPOINT": "http://seaweedfs:8333",
        "CLOSE_S3_ACCESS_KEY": "reader-key",
        "CLOSE_S3_SECRET_KEY": "reader-secret",
        "EMULATOR_JARS_DIR": "/nonexistent",
    }
    cmd, child_env = command(env)
    assert cmd[:3] == ["spark-submit", "--class", SERVER_CLASS]
    assert cmd[-1] == "spark-internal"
    confs = dict(cmd[i + 1].split("=", 1) for i, arg in enumerate(cmd) if arg == "--conf")
    for key, value in PINNED_SPARK_CONF.items():
        assert confs[key] == value
    assert confs["spark.scheduler.mode"] == "FAIR"
    assert "reader-secret" not in " ".join(cmd)  # credentials travel as environment, never argv
    assert child_env["AWS_SECRET_ACCESS_KEY"] == "reader-secret"


def test_jobs_and_the_connect_server_are_sized_for_their_two_cores() -> None:
    from close_jobs_emulator.__main__ import JVM_FLAGS
    from close_jobs_emulator.connect_server import command

    cmd, _ = command({"EMULATOR_JARS_DIR": "/nonexistent"})
    confs = dict(cmd[i + 1].split("=", 1) for i, arg in enumerate(cmd) if arg == "--conf")
    assert cmd[cmd.index("--master") + 1] == "local[2]"
    assert confs["spark.driver.extraJavaOptions"] == JVM_FLAGS
    assert JVM_FLAGS.count("-XX:ActiveProcessorCount=2") == 1


def test_default_job_ids_never_collide_with_configured_ones() -> None:
    path = Path(__file__).resolve().parents[3] / "engine" / "databricks.yml"
    jobs = load_bundle(path, job_ids={"smoke": 1001})
    assert jobs[1001].key == "smoke"
    assert sorted(j.key for j in jobs.values()) == [
        "datagen",
        "ddl_apply",
        "journal_ref",
        "post_journals",
        "smoke",
    ]
    with pytest.raises(BundleError, match="repeat"):
        load_bundle(path, job_ids={"smoke": 1001, "datagen": 1001})
