"""The published sample request bodies equal the sample configuration (a drift check, like the schemas)."""

from __future__ import annotations

from tools import gen_journal_samples


def test_published_samples_match_the_code() -> None:
    assert gen_journal_samples.main(["--check"]) == 0
