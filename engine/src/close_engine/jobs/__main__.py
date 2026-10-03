"""Dispatcher: ``close-job <kind> [job args...]`` / ``python -m close_engine.jobs <kind> ...``."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence

from close_engine.jobs import JOB_MODULES


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in JOB_MODULES:
        known = ", ".join(sorted(JOB_MODULES))
        sys.stderr.write(f"usage: close-job <kind> [args...]  (kinds: {known})\n")
        return 2
    module = importlib.import_module(JOB_MODULES[args[0]])
    result: int = module.main(args[1:])
    return result


if __name__ == "__main__":
    raise SystemExit(main())
