"""A journaling run over all events, with state from earlier runs (ADR-0015).

- **Corrections and reruns.** Each event is booked once per version. An unchanged event (same row hash as
  booked) books nothing. A changed one gets the next version: its booked lines are reversed, sides flipped,
  and new lines booked. An event whose source row is gone is reversed and left empty (voided).
- **Configuration errors.** An event type with a configuration problem fails outright. None of its events
  book in this run, and its state stays as it was, so the next run retries it.
- **Circuit breaker.** More than ``breaker_bps`` (0.5%) of the run's journaled events in suspense fails the run.
- **Rollups.** Lines are summarized by entity, period, account, dimensions and currency. Each summary has a
  deterministic id, which every line carries for drill-down.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

from close_contracts.ids import hash_id
from close_engine.reference.journal.events import Event
from close_engine.reference.journal.posting import (
    ConfigError,
    JournalConfig,
    Line,
    ReferenceData,
    _period,
    compile_rules,
    post,
)


class CircuitBreakerError(Exception):
    """Too many events went to suspense: the run commits nothing."""


@dataclass(frozen=True)
class Booked:
    event_type: str
    version: int
    row_hash: str  # empty once the event is voided
    lines: tuple[Line, ...]


@dataclass
class LedgerState:
    events: dict[str, Booked] = field(default_factory=dict)


@dataclass
class RunResult:
    lines: list[Line]  # what this run booked: new lines and reversals
    state: LedgerState
    counts: Counter[str]
    failed_types: dict[str, str]


def _reverse(ref: ReferenceData, booked: Booked, version: int) -> list[Line]:
    out = []
    for ln in booked.lines:
        period, ppa, original = _period(ref, ln.entity_id, ln.accounting_date)
        out.append(
            replace(
                ln,
                line_id=hash_id("line", ln.event_id, str(version), "reversal", ln.line_id),
                event_version=version,
                side="CR" if ln.side == "DR" else "DR",
                period=period,
                is_prior_period_adjustment=ppa,
                original_period=original,
                is_reversal=True,
            )
        )
    return out


def run(
    config: JournalConfig,
    events: Sequence[Event],
    *,
    ref: ReferenceData,
    state: LedgerState | None = None,
    breaker_bps: int = 50,
) -> RunResult:
    rules = compile_rules(config)
    prior = state.events if state is not None else {}
    lines_by_type: dict[str, list[Line]] = defaultdict(list)
    updates: dict[str, dict[str, Booked]] = defaultdict(dict)
    failed: dict[str, str] = {}
    counts: Counter[str] = Counter()
    seen = set()
    for event in sorted(events, key=lambda e: e.event_id):
        seen.add(event.event_id)
        if event.event_type in failed:
            continue
        before = prior.get(event.event_id)
        if before is not None and before.row_hash == event.row_hash:
            counts["unchanged"] += 1
            continue
        version = before.version + 1 if before is not None else 1
        try:
            posted = post(config, rules, ref, event, version)
        except ConfigError as exc:
            failed[event.event_type] = str(exc)
            continue
        if before is not None:
            lines_by_type[event.event_type] += _reverse(ref, before, version)
            counts["corrected"] += 1
        lines_by_type[event.event_type] += posted.lines
        updates[event.event_type][event.event_id] = Booked(
            event.event_type, version, event.row_hash, tuple(posted.lines)
        )
        counts[posted.status] += 1
    for event_id, before in sorted(prior.items()):
        if event_id not in seen and before.row_hash:
            version = before.version + 1
            lines_by_type[before.event_type] += _reverse(ref, before, version)
            updates[before.event_type][event_id] = Booked(before.event_type, version, "", ())
            counts["voided"] += 1
    journaled = counts["posted"] + counts["suspended"]
    if journaled and counts["suspended"] * 10_000 > breaker_bps * journaled:
        raise CircuitBreakerError(f"{counts['suspended']} of {journaled} journaled events went to suspense")
    new_state = dict(prior)
    lines: list[Line] = []
    for event_type, type_lines in lines_by_type.items():
        if event_type not in failed:
            lines += type_lines
            new_state.update(updates[event_type])
    counts["failed_types"] = len(failed)
    return RunResult(sorted(lines, key=lambda ln: ln.line_id), LedgerState(new_state), counts, failed)


def all_lines(state: LedgerState) -> list[Line]:
    """Every line currently booked: the lines of each event's latest version (reversed versions net to zero)."""
    return sorted((ln for b in state.events.values() for ln in b.lines), key=lambda ln: ln.line_id)


def dimensions_key(dimensions: Iterable[tuple[str, str]]) -> str:
    return ";".join(f"{k}={v}" for k, v in sorted(dimensions))


@dataclass(frozen=True)
class RollupLine:
    rollup_line_id: str
    entity_id: str
    period: str
    account: str
    dimensions_key: str
    currency: str
    debit_minor: int
    credit_minor: int
    functional_debit_minor: int
    functional_credit_minor: int
    line_count: int


def rollup(lines: Iterable[Line]) -> tuple[list[RollupLine], dict[str, str]]:
    """GL summary lines, and each subledger line's rollup id."""
    groups: dict[tuple[str, str, str, str, str], list[Line]] = defaultdict(list)
    for ln in lines:
        groups[(ln.entity_id, ln.period, ln.account, dimensions_key(ln.dimensions), ln.currency)].append(ln)
    out: list[RollupLine] = []
    links: dict[str, str] = {}
    for key, members in sorted(groups.items()):
        rid = hash_id("rollup", *key)
        dr = [m for m in members if m.side == "DR"]
        cr = [m for m in members if m.side == "CR"]
        out.append(
            RollupLine(
                rid,
                *key,
                sum(m.amount_minor for m in dr),
                sum(m.amount_minor for m in cr),
                sum(m.functional_amount_minor for m in dr),
                sum(m.functional_amount_minor for m in cr),
                len(members),
            )
        )
        links |= {m.line_id: rid for m in members}
    return out, links


def _line_record(ln: Line) -> str:
    record = [
        ln.line_id,
        ln.event_version,
        ln.account,
        ln.side,
        ln.amount_minor,
        ln.currency,
        ln.functional_amount_minor,
        ln.period,
        list(ln.dimensions),
        ln.is_reversal,
    ]
    return json.dumps(record, default=str)


def digest(lines: Iterable[Line]) -> str:
    """An order-independent digest of booked lines."""
    return hashlib.sha256("\n".join(sorted(_line_record(ln) for ln in lines)).encode()).hexdigest()
