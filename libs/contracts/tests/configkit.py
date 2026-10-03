"""Hypothesis strategies that generate valid DSL expressions and configuration models."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from hypothesis import strategies as st

from close_contracts.config.accounting_policy import (
    AccountingPolicy,
    IncentiveTreatment,
    Presentation,
    Program,
    ProgramModel,
    ProgramPolicy,
    ShippingTreatment,
    Trigger,
)
from close_contracts.config.data_source import DataSource, Incremental, SignConvention
from close_contracts.config.envelope import ConfigKind, ConfigStatus, Envelope
from close_contracts.config.match_set import (
    AmountTolerance,
    CurrencyRule,
    DateWindow,
    ExpressionCondition,
    KeyEquals,
    Limits,
    MatchSet,
    Owners,
    PassRule,
    Role,
    RuleKind,
    Schedule,
    SignRule,
    SourceBinding,
    TextMatch,
    TimingRule,
)
from close_contracts.dsl.ast import Call, Col, DslType, Lit

TEXT = st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), max_size=40)
slugs = st.from_regex(r"[a-z][a-z0-9_]{2,20}", fullmatch=True)
principals = st.from_regex(r"[A-Za-z0-9._@-]{1,20}", fullmatch=True)
fields = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)
calendars = st.sampled_from(["US_FED", "TARGET2", "UK_BANK"])

columns = st.builds(Col, col=st.from_regex(r"(?:[abc]\.)?[A-Za-z_][A-Za-z0-9_]{0,15}", fullmatch=True))
literals = st.builds(
    Lit,
    lit=st.one_of(st.none(), st.booleans(), st.integers(-(2**53 - 1), 2**53 - 1), TEXT),
    type=st.sampled_from(list(DslType)),
)
exprs = st.recursive(
    st.one_of(columns, literals),
    lambda inner: st.builds(
        Call, fn=st.from_regex(r"[a-z][a-z0-9_]{0,12}", fullmatch=True), args=st.lists(inner, max_size=4).map(tuple)
    ),
    max_leaves=12,
)


def _key() -> st.SearchStrategy[KeyEquals]:
    return st.builds(KeyEquals, left=exprs, right=exprs)


conditions: st.SearchStrategy[Any] = st.one_of(
    _key(),
    st.builds(TextMatch, op=st.sampled_from(["equals", "normalized_equals"]), left=exprs, right=exprs),
    st.builds(
        TextMatch,
        op=st.sampled_from(["contains", "starts_with", "ends_with"]),
        left=exprs,
        right=exprs,
        min_needle_length=st.integers(1, 256),
    ),
    st.builds(
        TextMatch,
        op=st.just("regex_extract_equals"),
        left=exprs,
        right=exprs,
        pattern=st.sampled_from([r"TX(\d{8})XT", r"pmt-([A-Za-z0-9]+)", r"(?i)^ref(\d+)"]),
        group=st.integers(0, 1),
    ),
    st.builds(
        TextMatch,
        op=st.just("fuzzy"),
        left=exprs,
        right=exprs,
        algorithm=st.sampled_from(["levenshtein", "jaro_winkler"]),
        min_similarity_bps=st.integers(0, 10_000),
    ),
    st.builds(
        DateWindow,
        left=exprs,
        right=exprs,
        unit=st.just("calendar_days"),
        before=st.integers(0, 366),
        after=st.integers(0, 366),
    ),
    st.builds(
        DateWindow,
        left=exprs,
        right=exprs,
        unit=st.just("business_days"),
        before=st.integers(0, 366),
        after=st.integers(0, 366),
        calendar=calendars,
    ),
    st.builds(AmountTolerance, left=exprs, right=exprs, mode=st.just("exact")),
    st.builds(AmountTolerance, left=exprs, right=exprs, mode=st.just("absolute"), absolute_minor=st.integers(1, 10**9)),
    st.builds(AmountTolerance, left=exprs, right=exprs, mode=st.just("bps"), bps=st.integers(1, 10_000)),
    st.builds(
        AmountTolerance,
        left=exprs,
        right=exprs,
        mode=st.just("bps_capped"),
        bps=st.integers(1, 10_000),
        cap_minor=st.integers(1, 10**9),
    ),
    st.builds(SignRule, mode=st.sampled_from(["same", "opposite", "absolute"])),
    st.builds(CurrencyRule, mode=st.just("equal")),
    st.builds(CurrencyRule, mode=st.just("fx_converted"), tolerance_bps=st.integers(0, 10_000)),
    st.builds(ExpressionCondition, expr=exprs),
)
limits = st.builds(
    Limits,
    max_candidates_per_item=st.integers(1, 1000),
    max_block_pairs=st.integers(1, 100_000_000),
    subset_max_candidates=st.integers(2, 24),
    work_budget=st.integers(1, 2**24),
)


@st.composite
def pass_rules(draw: st.DrawFn, roles: tuple[Role, ...]) -> PassRule:
    kind = draw(st.sampled_from(list(RuleKind)))
    if kind == RuleKind.SELF_NET:
        sides: tuple[Role, ...] = (draw(st.sampled_from(roles)),)
    else:
        pair = draw(st.permutations(roles)) if len(roles) >= 2 else None
        if pair is None:
            kind, sides = RuleKind.SELF_NET, (roles[0],)
        else:
            sides = (pair[0], pair[1])
    conds = [draw(_key())] if kind != RuleKind.SELF_NET else []
    conds += draw(st.lists(conditions, min_size=0 if conds else 1, max_size=4))
    greedy = draw(st.booleans())
    outcome = draw(st.sampled_from(["auto", "suggested"]))
    return PassRule(
        rule_id=draw(slugs),
        name=draw(st.text(min_size=1, max_size=30).filter(str.strip)),
        kind=kind,
        sides=sides,
        outcome=outcome,
        ambiguity="greedy_total_order" if greedy else draw(st.sampled_from(["unique_only", "best_unique"])),
        allow_ambiguous_auto=greedy and outcome == "auto" and draw(st.booleans()),
        conditions=tuple(conds),
        group_by=tuple(draw(st.lists(exprs, min_size=1, max_size=2)))
        if kind in {RuleKind.ONE_TO_MANY, RuleKind.MANY_TO_ONE}
        else (),
        limits=draw(limits),
    )


@st.composite
def match_sets(draw: st.DrawFn) -> MatchSet:
    roles = tuple(draw(st.lists(st.sampled_from(list(Role)), min_size=1, max_size=3, unique=True)))
    rules = draw(st.lists(pass_rules(roles), min_size=1, max_size=3, unique_by=lambda r: r.rule_id))
    used = {r.rule_id for r in rules}
    timing = draw(
        st.lists(
            st.builds(
                TimingRule,
                rule_id=slugs.filter(lambda x: x not in used),
                role=st.sampled_from(roles),
                when=exprs,
                expected_clear_business_days=st.integers(0, 60),
                calendar=calendars,
            ),
            max_size=2,
            unique_by=lambda t: t.rule_id,
        )
    )
    preparer, reviewer = draw(st.lists(principals, min_size=2, max_size=2, unique=True))
    daily = draw(st.booleans())
    return MatchSet(
        match_set_id=draw(slugs),
        name=draw(st.text(min_size=1, max_size=30).filter(str.strip)),
        kind=draw(st.sampled_from(["matching", "intercompany"])),
        entity_ids=tuple(
            draw(st.lists(st.from_regex(r"[A-Z0-9]{2,8}", fullmatch=True), min_size=1, max_size=4, unique=True))
        ),
        sources=tuple(SourceBinding(role=r, source_id=draw(slugs), scope=draw(st.none() | exprs)) for r in roles),
        pass_rules=tuple(rules),
        timing_rules=tuple(timing),
        owners=Owners(preparer=preparer, reviewer=reviewer),
        schedule=Schedule(mode="daily", at_utc=draw(st.from_regex(r"([01]\d|2[0-3]):[0-5]\d", fullmatch=True)))
        if daily
        else Schedule(mode="manual"),
    )


@st.composite
def data_sources(draw: st.DrawFn) -> DataSource:
    column_mode = draw(st.booleans())
    return DataSource(
        source_id=draw(slugs),
        description=draw(st.text(max_size=60)),
        landing_table="landing." + draw(fields),
        identity_key=tuple(
            draw(
                st.lists(
                    st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,15}", fullmatch=True), min_size=1, max_size=4, unique=True
                )
            )
        ),
        mapping=draw(st.dictionaries(fields, exprs, min_size=1, max_size=5)),
        filter=draw(st.none() | exprs),
        sign=draw(st.sampled_from(list(SignConvention))),
        incremental=Incremental(
            mode="column",
            column=draw(st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)),
            lookback_days=draw(st.integers(0, 90)),
        )
        if column_mode
        else Incremental(mode="cdf", lookback_days=draw(st.integers(0, 90))),
    )


@st.composite
def accounting_policies(draw: st.DrawFn) -> AccountingPolicy:
    programs = draw(st.lists(st.sampled_from(list(Program)), min_size=1, max_size=4, unique=True))
    policies = []
    for program in programs:
        triggers = [t for t in Trigger if t != Trigger.VERIFICATION_PASSED or program == Program.VERIFIED_MARKETPLACE]
        policies.append(
            ProgramPolicy(
                program=program,
                trigger=draw(st.sampled_from(triggers)),
                model=draw(st.sampled_from(list(ProgramModel))),
                shipping=draw(st.sampled_from(list(ShippingTreatment))),
                incentives=draw(st.sampled_from(list(IncentiveTreatment))),
            )
        )
    return AccountingPolicy(
        programs=tuple(policies),
        presentation=Presentation(
            psp_receivable=draw(st.sampled_from(["cash_equivalent", "accounts_receivable"])),
            processing_fees=draw(st.sampled_from(["cost_of_revenue", "sga"])),
            chargebacks=draw(st.sampled_from(["reserve", "transaction_loss"])),
        ),
    )


@st.composite
def envelopes(draw: st.DrawFn) -> Envelope[Any]:
    kind, spec = draw(
        st.one_of(
            st.tuples(st.just(ConfigKind.DATA_SOURCE), data_sources()),
            st.tuples(st.just(ConfigKind.MATCH_SET), match_sets()),
            st.tuples(st.just(ConfigKind.ACCOUNTING_POLICY), accounting_policies()),
        )
    )
    status = draw(st.sampled_from(list(ConfigStatus)))
    author, approver = draw(st.lists(principals, min_size=2, max_size=2, unique=True))
    start = draw(st.dates(date(2020, 1, 1), date(2035, 12, 31)))
    end = draw(st.none() | st.integers(1, 3650).map(lambda d: start + timedelta(days=d)))
    needs = status in {ConfigStatus.APPROVED, ConfigStatus.ACTIVE, ConfigStatus.RETIRED}
    return Envelope[type(spec)].seal(  # type: ignore[misc]
        spec,
        kind=kind,
        config_id=draw(slugs),
        version=draw(st.integers(1, 10_000)),
        status=status,
        effective_from=start,
        effective_to=end,
        author=author,
        approver=approver if needs else None,
    )
