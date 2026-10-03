"""Strict parser for the job definitions in the engine bundle (``engine/databricks.yml``).

Only the subset the emulator understands is accepted: job parameters, exactly one
``python_wheel_task``, timeout, ``max_concurrent_runs``, queueing and ``max_retries: 0``. Unknown keys
are rejected rather than silently ignored, and targets may not override job parameters or tasks, so a
local run can never execute a different job than the one deployed to Databricks.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

ALLOWED_TOP: Final = {
    "bundle",
    "variables",
    "artifacts",
    "resources",
    "targets",
    "include",
    "workspace",
    "sync",
    "permissions",
}
ALLOWED_JOB: Final = {
    "name",
    "description",
    "max_concurrent_runs",
    "timeout_seconds",
    "queue",
    "parameters",
    "tasks",
    "job_clusters",
    "environments",
    "tags",
    "email_notifications",
    "permissions",
}
ALLOWED_TASK: Final = {
    "task_key",
    "description",
    "max_retries",
    "timeout_seconds",
    "python_wheel_task",
    "libraries",
    "job_cluster_key",
    "environment_key",
}
FORBIDDEN_TARGET_OVERRIDES: Final = {"parameters", "tasks", "timeout_seconds", "max_concurrent_runs", "queue"}
_VAR_RE: Final = re.compile(r"\$\{var\.([a-z_][a-z0-9_]*)\}")
_PARAM_RE: Final = re.compile(r"\{\{job\.parameters\.([a-z_][a-z0-9_]*)\}\}")
_JOB_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,13}$")


class BundleError(ValueError):
    pass


@dataclass(frozen=True)
class JobParam:
    name: str
    default: str


@dataclass(frozen=True)
class WheelTask:
    task_key: str
    package_name: str
    entry_point: str
    parameters: tuple[str, ...]


@dataclass(frozen=True)
class JobDefinition:
    key: str
    job_id: int
    name: str
    parameters: tuple[JobParam, ...]
    task: WheelTask
    timeout_seconds: int
    max_concurrent_runs: int
    queue_enabled: bool

    def defaults(self) -> dict[str, str]:
        return {p.name: p.default for p in self.parameters}

    def render_args(self, values: Mapping[str, str]) -> list[str]:
        """Substitute ``{{job.parameters.x}}`` in the wheel task parameters."""
        merged = {**self.defaults(), **values}

        def sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in merged:
                raise BundleError(f"task references unknown job parameter {name!r}")
            return merged[name]

        return [_PARAM_RE.sub(sub, arg) for arg in self.task.parameters]


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise BundleError(message)


def _resolve_vars(value: str, variables: Mapping[str, str]) -> str:
    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        _require(name in variables, f"undefined bundle variable {name!r}")
        return variables[name]

    return _VAR_RE.sub(sub, value)


def _variables(raw: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, spec in (raw.get("variables") or {}).items():
        default = spec.get("default") if isinstance(spec, Mapping) else None
        if default is not None:
            out[name] = str(default)
    for name, value in (target.get("variables") or {}).items():
        out[name] = str(value.get("default") if isinstance(value, Mapping) else value)
    return out


def _check_targets(raw: Mapping[str, Any]) -> None:
    for tname, target in (raw.get("targets") or {}).items():
        jobs = (((target or {}).get("resources") or {}).get("jobs") or {}) if isinstance(target, Mapping) else {}
        for jkey, override in jobs.items():
            bad = FORBIDDEN_TARGET_OVERRIDES & set(override or {})
            _require(not bad, f"target {tname!r} overrides {sorted(bad)} of job {jkey!r}; that is not allowed")


def _parse_job(key: str, spec: Mapping[str, Any], job_id: int, variables: Mapping[str, str]) -> JobDefinition:
    _require(bool(_JOB_KEY_RE.fullmatch(key)), f"job key {key!r} must match {_JOB_KEY_RE.pattern}")
    unknown = set(spec) - ALLOWED_JOB
    _require(not unknown, f"job {key!r} has unsupported keys {sorted(unknown)}")
    tasks = spec.get("tasks") or []
    _require(len(tasks) == 1, f"job {key!r} must have exactly one task")
    task = tasks[0]
    unknown = set(task) - ALLOWED_TASK
    _require(not unknown, f"task of job {key!r} has unsupported keys {sorted(unknown)}")
    _require(task.get("max_retries", 0) == 0, f"job {key!r}: max_retries must be 0 (retries belong to the app)")
    wheel = task.get("python_wheel_task")
    _require(isinstance(wheel, Mapping), f"job {key!r}: only python_wheel_task is supported")
    params = []
    for p in spec.get("parameters") or []:
        _require(
            isinstance(p, Mapping) and {"name", "default"} <= set(p), f"job {key!r}: parameters need name and default"
        )
        params.append(JobParam(str(p["name"]), _resolve_vars(str(p["default"]), variables)))
    names = [p.name for p in params]
    _require(len(set(names)) == len(names), f"job {key!r}: duplicate parameter names")
    task_args = tuple(str(a) for a in wheel.get("parameters") or [])
    for arg in task_args:
        for ref in _PARAM_RE.findall(arg):
            _require(ref in names, f"job {key!r}: task references undeclared parameter {ref!r}")
    timeout = int(spec.get("timeout_seconds", 0))
    _require(timeout > 0, f"job {key!r}: timeout_seconds must be > 0")
    return JobDefinition(
        key=key,
        job_id=job_id,
        name=str(spec.get("name", key)),
        parameters=tuple(params),
        task=WheelTask(str(task["task_key"]), str(wheel["package_name"]), str(wheel["entry_point"]), task_args),
        timeout_seconds=timeout,
        max_concurrent_runs=int(spec.get("max_concurrent_runs", 1)),
        queue_enabled=bool((spec.get("queue") or {}).get("enabled", False)),
    )


def load_bundle(
    path: Path, *, target: str = "local", job_ids: Mapping[str, int] | None = None
) -> dict[int, JobDefinition]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    unknown = set(raw) - ALLOWED_TOP
    _require(not unknown, f"bundle has unsupported top-level keys {sorted(unknown)}")
    _check_targets(raw)
    target_spec = ((raw.get("targets") or {}).get(target)) or {}
    variables = _variables(raw, target_spec)
    jobs_raw = ((raw.get("resources") or {}).get("jobs")) or {}
    _require(bool(jobs_raw), "bundle defines no jobs")
    ids = dict(job_ids or {})
    _require(len(set(ids.values())) == len(ids), f"job ids repeat in {ids}")
    used = set(ids.values())
    next_free = 1001
    jobs: dict[int, JobDefinition] = {}
    for key in sorted(jobs_raw):
        job_id = ids.get(key)
        if job_id is None:  # the next id no explicitly configured job uses, so defaults never collide
            while next_free in used:
                next_free += 1
            job_id = next_free
            used.add(job_id)
        jobs[job_id] = _parse_job(key, jobs_raw[key], job_id, variables)
    return jobs
