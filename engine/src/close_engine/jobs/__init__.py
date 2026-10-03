"""Engine job entrypoints. Each job module exposes ``main(argv: Sequence[str] | None) -> int``.

The same entrypoint runs everywhere:
  * Databricks: ``python_wheel_task`` (entry point ``close-job``) or the thin notebook in engine/notebooks
  * local k8s Job / spark-submit: ``engine/scripts/run_job.py <kind> ...``
  * tests: ``python -m close_engine.jobs <kind> ...`` or an in-process ``main([...])`` call
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

JOB_MODULES: Final = MappingProxyType(
    {
        "smoke": "close_engine.jobs.smoke",
        "ddl_apply": "close_engine.jobs.ddl_apply",
        "post_journals": "close_engine.jobs.post_journals",  # Stage 7b
        "journal_ref": "close_engine.jobs.journal_reference",  # Stage 8a: the sample journaling stand-in
        "datagen": "close_datagen.job",  # test data only: the close-datagen wheel is installed where it runs
    }
)

RESULT_PREFIX: Final = "CLOSE_JOB_RESULT "
"""Prefix of the single stdout line that carries a job's JSON result."""
