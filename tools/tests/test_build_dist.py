"""Merging two ``uv export`` outputs (tools/build_dist.py): the same pin under different markers merges into the
broader requirement, and a real disagreement still stops the build."""

from __future__ import annotations

import pytest

from tools.build_dist import merge

HASHES = "    --hash=sha256:aaaa \\\n    --hash=sha256:bbbb\n"
EVERYWHERE = "tzdata==2026.4 \\\n" + HASHES + "    # via pandas\n"
WINDOWS = "tzdata==2026.4 ; sys_platform == 'win32' \\\n" + HASHES + "    # via psycopg\n"


@pytest.mark.parametrize("order", [(EVERYWHERE, WINDOWS), (WINDOWS, EVERYWHERE)], ids=["first", "second"])
def test_an_unconditional_pin_covers_a_conditional_one(order: tuple[str, str]) -> None:
    out = merge(*order)
    assert out.splitlines()[0] == "tzdata==2026.4 \\"
    assert out.count("tzdata==") == 1


@pytest.mark.parametrize(
    "other",
    [
        "tzdata==2026.5 \\\n" + HASHES,  # another version
        "tzdata==2026.4 \\\n    --hash=sha256:cccc\n",  # other hashes
        "tzdata==2026.4 ; sys_platform == 'darwin' \\\n" + HASHES,  # two different conditions
    ],
    ids=["version", "hashes", "markers"],
)
def test_a_real_disagreement_stops_the_build(other: str) -> None:
    with pytest.raises(SystemExit, match="exports disagree on tzdata"):
        merge(WINDOWS, other)
